# /opt/box/nas.py
from __future__ import annotations
import datetime as _dt
import json, logging, os, shlex, shutil, subprocess, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

# Wir nutzen storage-Helfer für Pfad-Auflösung
import storage


@dataclass
class NASConfig:
    host: str
    username: str
    remote_base_dir: str
    port: int = 22
    key_path: str = "/opt/box/.ssh/id_ed25519_nas"
    retention_days: int = 14


class NASManager:
    """
    KISS-Manager für:
      - Setup (einmalig): Key erzeugen, autorisieren, Basisordner anlegen
      - Health: SSH-Key-Login per BatchMode testen
      - Upload: rsync mit SSH-Key, idempotent
      - Retention: lokale Runs nach erfolgreichem Upload + Frist löschen
    """

    def __init__(self, runs_root: Path, config_path: Path, logger: Optional[logging.Logger] = None) -> None:
        self.runs_root = runs_root
        self.config_path = config_path
        self.log = logger or logging.getLogger("nas")
        self._lock = threading.Lock()
        self._uploading: set[str] = set()
        self._health_state: Dict[str, Any] = {"ok": False, "last_checked": None, "message": "not checked"}

        key_dir = Path("/opt/box/.ssh")
        key_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Config ----------
    def _load_config(self) -> Optional[NASConfig]:
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return NASConfig(**data)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self.log.warning("Failed to load NAS config: %s", exc)
            return None

    def _write_config(self, cfg: NASConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg.__dict__, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.config_path)

    # ---------- Setup ----------
    def setup(self, *, host: str, port: int, username: str, password: str,
              remote_base_dir: str, retention_days: int = 14) -> Dict[str, Any]:
        if not host or not username or not password:
            raise HTTPException(400, "host/username/password erforderlich")
        if not remote_base_dir or " " in remote_base_dir:
            raise HTTPException(400, "remote_base_dir erforderlich und ohne Leerzeichen")

        key_path = Path("/opt/box/.ssh/id_ed25519_nas")
        pub_path = key_path.with_suffix(".pub")

        if not key_path.exists():
            # ed25519-Key erzeugen (leer passphrase)
            self._run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True)
            key_path.chmod(0o600)
        if not pub_path.exists():
            raise HTTPException(500, "Public key fehlt nach ssh-keygen")

        pubkey = pub_path.read_text(encoding="utf-8").strip()

        # Nur für Setup: per Passwort mit Paramiko verbinden und Key autorisieren
        try:
            import paramiko  # type: ignore
        except Exception as exc:
            raise HTTPException(500, "Paramiko nicht installiert. Bitte: pip install paramiko") from exc

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=host, port=int(port or 22), username=username, password=password, timeout=20)

            # ~/.ssh vorbereiten + authorized_keys erweitern (idempotent)
            cmds = [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                f"grep -qxF {shlex.quote(pubkey)} ~/.ssh/authorized_keys || "
                f"echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
                f"mkdir -p {shlex.quote(remote_base_dir)}",
            ]
            for cmd in cmds:
                stdin, stdout, stderr = client.exec_command(cmd)
                rc = stdout.channel.recv_exit_status()
                if rc != 0:
                    msg = stderr.read().decode("utf-8", errors="ignore").strip()
                    raise HTTPException(502, f"Remote-Setup fehlgeschlagen: {cmd} -> {msg}")

        finally:
            try:
                client.close()
            except Exception:
                pass

        cfg = NASConfig(
            host=host,
            port=int(port or 22),
            username=username,
            remote_base_dir=remote_base_dir,
            key_path=str(key_path),
            retention_days=int(retention_days or 14),
        )
        self._write_config(cfg)

        # Erste Health-Prüfung via Key-Login
        ok, msg = self._probe(cfg)
        return {"ok": bool(ok), "message": msg or ("SSH key login OK" if ok else "Probe fehlgeschlagen")}

    # ---------- Health ----------
    def health(self) -> Dict[str, Any]:
        cfg = self._load_config()
        if not cfg:
            self._health_state = {"ok": False, "last_checked": _dt.datetime.utcnow().isoformat()+"Z", "message": "not configured"}
            return dict(self._health_state)

        ok, msg = self._probe(cfg)
        self._health_state = {"ok": bool(ok), "last_checked": _dt.datetime.utcnow().isoformat()+"Z", "message": msg or ""}
        return dict(self._health_state)

    def _probe(self, cfg: NASConfig) -> tuple[bool, str]:
        cmd = [
            "ssh",
            "-i", cfg.key_path,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-p", str(cfg.port),
            f"{cfg.username}@{cfg.host}",
            "true",
        ]
        res = self._run(cmd, check=False)
        if res.returncode == 0:
            return True, "ok"
        return False, f"ssh probe failed (rc={res.returncode})"

    # ---------- Upload ----------
    def enqueue_upload(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._uploading:
                return False
            self._uploading.add(run_id)
        t = threading.Thread(target=self._upload_worker, args=(run_id,), daemon=True, name=f"upload-{run_id}")
        t.start()
        return True

    def _upload_worker(self, run_id: str) -> None:
        cfg = self._load_config()
        if not cfg:
            self.log.warning("Upload skipped: NAS not configured (run_id=%s)", run_id)
            with self._lock:
                self._uploading.discard(run_id)
            return

        try:
            run_dir = storage.resolve_run_directory(run_id)
        except HTTPException:
            self.log.error("Upload skipped: run_id not found (%s)", run_id)
            with self._lock:
                self._uploading.discard(run_id)
            return

        # Zielstruktur: <remote_base_dir>/<relative_to_runs_root>
        try:
            rel = run_dir.relative_to(self.runs_root).as_posix()
        except Exception:
            # Fallback: in einen Unterordner nach run_id
            rel = run_id

        dest = f"{cfg.remote_base_dir.rstrip('/')}/{rel}"
        ok, msg = self._mkdir_remote(cfg, dest)
        if not ok:
            self._mark_failed(run_dir, reason=f"mkdir remote failed: {msg}")
            with self._lock:
                self._uploading.discard(run_id)
            return

        # rsync Upload (idempotent)
        ssh_cmd = f"ssh -i {shlex.quote(cfg.key_path)} -o BatchMode=yes -o StrictHostKeyChecking=no -p {cfg.port}"
        rsync_cmd = [
            "rsync", "-a", "--partial", "--append-verify",
            "-e", ssh_cmd,
            str(run_dir) + "/",  # trailing slash = Inhalt kopieren
            f"{cfg.username}@{cfg.host}:{dest}/",
        ]
        res = self._run(rsync_cmd, check=False)
        if res.returncode != 0:
            self._mark_failed(run_dir, reason=f"rsync rc={res.returncode}")
        else:
            # Kleinste Verifikation: Anzahl Dateien vergleichen
            local_count = sum(1 for p in run_dir.rglob("*") if p.is_file())
            remote_cnt_cmd = [
                "ssh", "-i", cfg.key_path, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-p", str(cfg.port),
                f"{cfg.username}@{cfg.host}", "bash", "-lc", f"find {shlex.quote(dest)} -type f | wc -l",
            ]
            cnt = self._run(remote_cnt_cmd, check=False)
            if cnt.returncode == 0:
                try:
                    remote_count = int((cnt.stdout or "").strip())
                except Exception:
                    remote_count = -1
            else:
                remote_count = -1

            if remote_count >= 0 and remote_count < local_count:
                self._mark_failed(run_dir, reason=f"verify mismatch local={local_count} remote={remote_count}")
            else:
                (run_dir / "UPLOAD_DONE").write_text(_dt.datetime.utcnow().isoformat()+"Z", encoding="utf-8")
                self.log.info("Upload OK run_id=%s dest=%s", run_id, dest)

        with self._lock:
            self._uploading.discard(run_id)

    def _mkdir_remote(self, cfg: NASConfig, dest: str) -> tuple[bool, str]:
        cmd = [
            "ssh", "-i", cfg.key_path, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-p", str(cfg.port),
            f"{cfg.username}@{cfg.host}",
            "mkdir", "-p", dest,
        ]
        res = self._run(cmd, check=False)
        return (res.returncode == 0, f"rc={res.returncode}")

    def _mark_failed(self, run_dir: Path, reason: str) -> None:
        self.log.warning("Upload FAILED dir=%s reason=%s", run_dir, reason)
        (run_dir / "upload_failed").write_text(reason, encoding="utf-8")

    # ---------- Retention ----------
    def start_background(self) -> None:
        # Initiale Health-Probe (3 Versuche, non-blocking)
        threading.Thread(target=self._initial_health_probe, daemon=True, name="nas-health-probe").start()
        # Housekeeper
        threading.Thread(target=self._retention_loop, daemon=True, name="nas-retention").start()

    def _initial_health_probe(self) -> None:
        cfg = self._load_config()
        if not cfg:
            return
        for _ in range(3):
            ok, msg = self._probe(cfg)
            self._health_state = {"ok": bool(ok), "last_checked": _dt.datetime.utcnow().isoformat()+"Z", "message": msg or ""}
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
            time.sleep(6 * 3600)  # alle 6 Stunden

    def _apply_retention(self, cfg: NASConfig) -> None:
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=cfg.retention_days)
        for path in self.runs_root.rglob("*"):
            if not path.is_dir():
                continue
            # Nur Run-Verzeichnisse mit Upload-Marker
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

    # ---------- Utils ----------
    def _run(self, cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
        self.log.debug("RUN %s", " ".join(shlex.quote(c) for c in cmd))
        return subprocess.run(cmd, text=True, capture_output=True, check=check)
