# /opt/box/nas_smb.py
from __future__ import annotations
import datetime as _dt
import json, logging, os, shlex, shutil, subprocess, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

import storage  # benutzt resolve_run_directory & RUNS_ROOT-Spiegelung


@dataclass
class SMBConfig:
    host: str                # z.B. 192.168.1.10 oder nas.local
    share: str               # SMB-Share-Name, z.B. "experiments"
    username: str
    cred_path: str           # Pfad zur Credentials-Datei (0600)
    base_subdir: str = ""    # Unterordner innerhalb des Shares (optional)
    mount_root: str = "/mnt/nas_box"
    retention_days: int = 14
    cifs_vers: str = "3.0"   # SMB3
    domain: Optional[str] = None


class NASManager:
    """
    KISS-SMB-Manager:
      - setup(): Credentials schreiben, Mount prüfen, Basisordner anlegen
      - health(): Probe-Mount durchführen
      - enqueue_upload(): Upload-Worker per rsync in gemountetes Ziel
      - start_background(): Health-Probe + Retention-Loop
    """

    def __init__(self, runs_root: Path, config_path: Path, logger: Optional[logging.Logger] = None) -> None:
        self.runs_root = runs_root
        self.config_path = config_path
        self.log = logger or logging.getLogger("nas_smb")
        self._upl_lock = threading.Lock()
        self._mnt_lock = threading.Lock()
        self._uploading: set[str] = set()
        self._health_state: Dict[str, Any] = {"ok": False, "last_checked": None, "message": "not checked"}
        Path("/opt/box").mkdir(parents=True, exist_ok=True)
        Path("/mnt/nas_box").mkdir(parents=True, exist_ok=True)

    # ---------- Config ----------
    def _load_config(self) -> Optional[SMBConfig]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return SMBConfig(**data)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self.log.warning("Failed to load SMB config: %s", exc)
            return None

    def _write_config(self, cfg: SMBConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg.__dict__, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.config_path)

    def _write_credentials(self, path: Path, username: str, password: str, domain: Optional[str]) -> None:
        lines = [f"username={username}", f"password={password}"]
        if domain:
            lines.append(f"domain={domain}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)

    # ---------- Setup ----------
    def setup(self, *, host: str, share: str, username: str, password: str,
              base_subdir: str = "", retention_days: int = 14, domain: Optional[str] = None) -> Dict[str, Any]:
        if not (host and share and username and password):
            raise HTTPException(400, "host/share/username/password erforderlich")
        cred_path = Path("/opt/box/.smbcredentials_nas")
        self._write_credentials(cred_path, username=username, password=password, domain=domain)

        cfg = SMBConfig(
            host=host,
            share=share,
            username=username,
            cred_path=str(cred_path),
            base_subdir=(base_subdir or "").strip("/"),
            mount_root="/mnt/nas_box",
            retention_days=int(retention_days or 14),
            cifs_vers="3.0",
            domain=domain or None,
        )
        self._write_config(cfg)

        ok, msg = self._probe(cfg, ensure_base=True)
        return {"ok": bool(ok), "message": msg or ("SMB mount OK" if ok else "Probe fehlgeschlagen")}

    # ---------- Health ----------
    def health(self) -> Dict[str, Any]:
        cfg = self._load_config()
        if not cfg:
            self._health_state = {"ok": False, "last_checked": self._now(), "message": "not configured"}
            return dict(self._health_state)
        ok, msg = self._probe(cfg, ensure_base=False)
        self._health_state = {"ok": bool(ok), "last_checked": self._now(), "message": msg or ""}
        return dict(self._health_state)

    def _probe(self, cfg: SMBConfig, *, ensure_base: bool) -> tuple[bool, str]:
        mnt = Path(cfg.mount_root) / "health"
        try:
            self._mount(cfg, mnt, read_only=True)
            base_path = self._dest_base_path(cfg, mnt)
            if ensure_base:
                base_path.mkdir(parents=True, exist_ok=True)
            ok = base_path.exists()
            return (True, "ok") if ok else (False, f"base path not present: {base_path}")
        except Exception as exc:
            return False, f"probe error: {exc}"
        finally:
            try:
                self._umount(mnt)
            except Exception:
                pass

    # ---------- Upload ----------
    def enqueue_upload(self, run_id: str) -> bool:
        with self._upl_lock:
            if run_id in self._uploading:
                return False
            self._uploading.add(run_id)
        t = threading.Thread(target=self._upload_worker, args=(run_id,), daemon=True, name=f"smb-upload-{run_id}")
        t.start()
        return True

    def _upload_worker(self, run_id: str) -> None:
        cfg = self._load_config()
        if not cfg:
            self.log.warning("Upload skipped: SMB not configured (run_id=%s)", run_id)
            with self._upl_lock:
                self._uploading.discard(run_id)
            return

        try:
            run_dir = storage.resolve_run_directory(run_id)  # 404 wenn unbekannt
        except Exception as exc:
            self.log.error("Upload skipped: resolve_run_directory failed (%s): %s", run_id, exc)
            with self._upl_lock:
                self._uploading.discard(run_id)
            return

        mnt = Path(cfg.mount_root) / "upload"
        dest_base = None
        try:
            self._mount(cfg, mnt, read_only=False)
            dest_base = self._dest_base_path(cfg, mnt)

            # Ziel: <base>/<relativ_zu_RUNS_ROOT>
            try:
                rel = run_dir.relative_to(self.runs_root).as_posix()
            except Exception:
                rel = run_id
            dest = dest_base / rel
            dest.mkdir(parents=True, exist_ok=True)

            # rsync innerhalb des Dateisystems (lokal -> CIFS-Mount)
            rsync_cmd = [
                "rsync", "-a", "--partial",
                str(run_dir) + "/", str(dest) + "/",
            ]
            res = self._run(rsync_cmd, check=False)
            if res.returncode != 0:
                self._mark_failed(run_dir, f"rsync rc={res.returncode}, err={res.stderr.strip() if res.stderr else ''}")
            else:
                # minimale Verifikation: Dateizahl vergleichen
                local_count = sum(1 for p in run_dir.rglob("*") if p.is_file())
                remote_count = sum(1 for p in dest.rglob("*") if p.is_file())
                if remote_count < local_count:
                    self._mark_failed(run_dir, f"verify mismatch local={local_count} remote={remote_count}")
                else:
                    (run_dir / "UPLOAD_DONE").write_text(self._now(), encoding="utf-8")
                    self.log.info("SMB Upload OK run_id=%s dest=%s", run_id, dest)
        except Exception as exc:
            self._mark_failed(run_dir, f"upload error: {exc}")
        finally:
            try:
                self._umount(mnt)
            except Exception as exc:
                self.log.warning("umount failed: %s", exc)
            with self._upl_lock:
                self._uploading.discard(run_id)

    # ---------- Retention & Background ----------
    def start_background(self) -> None:
        threading.Thread(target=self._initial_health, daemon=True, name="smb-health-probe").start()
        threading.Thread(target=self._retention_loop, daemon=True, name="smb-retention").start()

    def _initial_health(self) -> None:
        cfg = self._load_config()
        if not cfg:
            return
        for _ in range(3):
            ok, msg = self._probe(cfg, ensure_base=False)
            self._health_state = {"ok": bool(ok), "last_checked": self._now(), "message": msg or ""}
            if ok:
                break
            time.sleep(5)

    def _retention_loop(self) -> None:
        while True:
            try:
                cfg = self._load_config()
                if cfg:
                    self._apply_retention(cfg)
            except Exception as exc:
                self.log.warning("retention pass failed: %s", exc)
            time.sleep(6 * 3600)

    def _apply_retention(self, cfg: SMBConfig) -> None:
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=cfg.retention_days)
        for path in self.runs_root.rglob("*"):
            if not path.is_dir():
                continue
            marker = path / "UPLOAD_DONE"
            if not marker.exists():
                continue
            try:
                ts = _dt.datetime.utcfromtimestamp(marker.stat().st_mtime)
            except Exception:
                ts = _dt.datetime.utcfromtimestamp(path.stat().st_mtime)
            if ts <= cutoff:
                try:
                    shutil.rmtree(path)
                    self.log.info("Local retention delete: %s", path)
                except Exception as exc:
                    self.log.warning("Failed to remove %s: %s", path, exc)

    # ---------- Mount helpers ----------
    def _unc(self, cfg: SMBConfig) -> str:
        return f"//{cfg.host.strip('/')}/{cfg.share.strip('/')}"

    def _dest_base_path(self, cfg: SMBConfig, mount_point: Path) -> Path:
        return mount_point / (cfg.base_subdir.strip("/") if cfg.base_subdir else "")

    def _mount(self, cfg: SMBConfig, mount_point: Path, *, read_only: bool) -> None:
        mount_point.mkdir(parents=True, exist_ok=True)
        opts = [
            f"credentials={cfg.cred_path}",
            f"vers={cfg.cifs_vers}",
            "iocharset=utf8",
            f"uid={os.getuid()}",
            f"gid={os.getgid()}",
            "file_mode=0644",
            "dir_mode=0755",
            "noserverino",
        ]
        if read_only:
            opts.append("ro")
        cmd = ["mount", "-t", "cifs", self._unc(cfg), str(mount_point), "-o", ",".join(opts)]
        with self._mnt_lock:
            # Wenn bereits gemountet, erst clean unmounten
            if mount_point.is_mount():
                self._umount(mount_point)
            res = self._run(cmd, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"mount failed rc={res.returncode} err={res.stderr.strip() if res.stderr else ''}")

    def _umount(self, mount_point: Path) -> None:
        if mount_point.exists():
            # lazy unmount, falls noch offene Handles
            self._run(["umount", "-l", str(mount_point)], check=False)

    # ---------- Utils ----------
    def _run(self, cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
        self.log.debug("RUN %s", " ".join(shlex.quote(c) for c in cmd))
        return subprocess.run(cmd, text=True, capture_output=True, check=check)

    @staticmethod
    def _now() -> str:
        return _dt.datetime.utcnow().isoformat() + "Z"
