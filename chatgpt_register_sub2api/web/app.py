"""FastAPI backend for the local web panel."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from chatgpt_register_sub2api.config import DEFAULT_CONFIG, DEFAULT_CONFIG_FILE, load_config
from chatgpt_register_sub2api.pipeline import (
    create_run_output_dir,
    load_accounts,
    run_full_pipeline,
)


PROJECT_ROOT = Path.cwd()
STATIC_DIR = Path(__file__).resolve().parent / "static"


class PanelConfig(BaseModel):
    config_path: str = "config.yaml"
    provider_type: str = "outlook_token"
    outlook_enabled: bool = True
    gmail_enabled: bool = False
    mailboxes: str = ""
    gmail_mailboxes: str = ""
    alias_enabled: bool = False
    alias_limit_per_mailbox: int = Field(default=5, ge=1, le=100)
    proxy_url: str = ""
    flaresolverr_url: str = ""
    workspace_ids: str = ""
    workspace_enabled: bool = True
    workspace_route: str = "k12_request"
    re_login_enabled: bool = False
    export_plan_type: str = "k12"
    sub2api_output_file: str = "sub2api_bundle.json"
    health_check: bool = True
    archive_runs: bool = True
    runs_dir: str = "runs"
    log_level: str = "INFO"


class RunRequest(BaseModel):
    config_path: str = "config.yaml"
    count: int = Field(default=1, ge=1, le=500)
    threads: int = Field(default=1, ge=1, le=100)
    workspace_ids: str = ""


@dataclass
class PanelJob:
    id: str
    status: str = "queued"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    logs: list[str] = field(default_factory=list)

    def append_log(self, message: str) -> None:
        text = str(message).rstrip()
        if not text:
            return
        self.logs.append(text)
        if len(self.logs) > 2000:
            self.logs = self.logs[-2000:]


class JobLogHandler(logging.Handler):
    def __init__(self, job: PanelJob) -> None:
        super().__init__()
        self.job = job
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.append_log(self.format(record))
        except Exception:
            pass


app = FastAPI(title="chatgpt-register-sub2api Panel")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_job_lock = threading.Lock()
_active_job: PanelJob | None = None
_last_job: PanelJob | None = None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _config_file(path: str | None = None) -> Path:
    selected = Path(path or DEFAULT_CONFIG_FILE)
    if not selected.is_absolute():
        selected = PROJECT_ROOT / selected
    return selected.resolve()


def _read_yaml_config(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    return _deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), raw)


def _provider(config: dict[str, Any], provider_type: str) -> dict[str, Any]:
    providers = config.setdefault("mail", {}).setdefault("providers", [])
    for item in providers:
        if isinstance(item, dict) and item.get("type") == provider_type:
            return item
    provider = {"type": provider_type, "enable": False, "mailboxes": ""}
    providers.append(provider)
    return provider


def _workspace_ids_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value)


def _workspace_ids_list(value: str) -> list[str]:
    ids: list[str] = []
    for line in str(value or "").replace(",", "\n").splitlines():
        text = line.strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _panel_config_from_dict(config: dict[str, Any], path: Path) -> PanelConfig:
    outlook = _provider(config, "outlook_token")
    gmail = _provider(config, "gmail_oauth")
    mail = config.get("mail", {})
    proxy = config.get("proxy", {})
    workspace = config.get("workspace", {})
    sub2api = config.get("sub2api", {})
    output = config.get("output", {})
    logging_cfg = config.get("logging", {})
    return PanelConfig(
        config_path=str(path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path),
        provider_type="gmail_oauth" if gmail.get("enable") and not outlook.get("enable") else "outlook_token",
        outlook_enabled=bool(outlook.get("enable", True)),
        gmail_enabled=bool(gmail.get("enable", False)),
        mailboxes=str(outlook.get("mailboxes") or ""),
        gmail_mailboxes=str(gmail.get("mailboxes") or ""),
        alias_enabled=bool(outlook.get("alias_enabled", mail.get("alias_enabled", False))),
        alias_limit_per_mailbox=int(outlook.get("alias_limit_per_mailbox") or 5),
        proxy_url=str(proxy.get("url") or ""),
        flaresolverr_url=str(proxy.get("flaresolverr_url") or ""),
        workspace_ids=_workspace_ids_text(workspace.get("ids", [])),
        workspace_enabled=bool(workspace.get("enabled", True)),
        workspace_route=str(workspace.get("route") or "k12_request"),
        re_login_enabled=bool(workspace.get("re_login_enabled", False)),
        export_plan_type=str(workspace.get("export_plan_type") or "k12"),
        sub2api_output_file=str(sub2api.get("output_file") or "sub2api_bundle.json"),
        health_check=bool(sub2api.get("health_check", True)),
        archive_runs=bool(output.get("archive_runs", True)),
        runs_dir=str(output.get("runs_dir") or "runs"),
        log_level=str(logging_cfg.get("level") or "INFO"),
    )


def _dict_from_panel_config(payload: PanelConfig) -> dict[str, Any]:
    path = _config_file(payload.config_path)
    config = _read_yaml_config(path)

    outlook = _provider(config, "outlook_token")
    gmail = _provider(config, "gmail_oauth")
    outlook.update(
        {
            "enable": payload.outlook_enabled,
            "label": outlook.get("label") or "Outlook Pool",
            "mode": outlook.get("mode") or "auto",
            "alias_enabled": payload.alias_enabled,
            "alias_limit_per_mailbox": payload.alias_limit_per_mailbox,
            "mailboxes": payload.mailboxes,
        }
    )
    gmail.update(
        {
            "enable": payload.gmail_enabled,
            "label": gmail.get("label") or "Gmail OAuth Pool",
            "imap_host": gmail.get("imap_host") or "imap.gmail.com",
            "message_limit": int(gmail.get("message_limit") or 10),
            "mailboxes": payload.gmail_mailboxes,
        }
    )

    config.setdefault("mail", {})["alias_enabled"] = payload.alias_enabled
    config.setdefault("mail", {})["alias_limit_per_mailbox"] = payload.alias_limit_per_mailbox
    config.setdefault("proxy", {})["url"] = payload.proxy_url.strip()
    config.setdefault("proxy", {})["flaresolverr_url"] = payload.flaresolverr_url.strip()
    config.setdefault("workspace", {}).update(
        {
            "enabled": payload.workspace_enabled,
            "ids": _workspace_ids_list(payload.workspace_ids),
            "route": payload.workspace_route.strip() or "k12_request",
            "re_login_enabled": payload.re_login_enabled,
            "export_plan_type": payload.export_plan_type.strip() or "k12",
        }
    )
    sub2api = config.setdefault("sub2api", {})
    sub2api["enabled"] = True
    sub2api["output_file"] = payload.sub2api_output_file.strip() or "sub2api_bundle.json"
    sub2api["health_check"] = payload.health_check
    config.setdefault("output", {})["archive_runs"] = payload.archive_runs
    config.setdefault("output", {})["runs_dir"] = payload.runs_dir.strip() or "runs"
    config.setdefault("logging", {})["level"] = payload.log_level.strip().upper() or "INFO"
    config.setdefault("logging", {})["file"] = ""
    config.pop("_config_dir", None)
    return config


def _apply_threads_override(config: dict[str, Any], threads: int) -> None:
    value = max(1, int(threads))
    config.setdefault("registration", {})["threads"] = value
    parallel = config.setdefault("parallel", {})
    parallel["join_threads"] = value
    parallel["refresh_threads"] = value
    parallel["login_threads"] = value


def _prepare_run_archive(config: dict[str, Any], count: int) -> tuple[Path | None, Path | None, Path | None]:
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict) or not bool(output_cfg.get("archive_runs", True)):
        config_dir = Path(config.get("_config_dir", "."))
        return None, config_dir / "registered_accounts.json", None

    run_dir = create_run_output_dir(config, count)
    accounts_file = run_dir / "registered_accounts.json"
    sub2api_cfg = config.get("sub2api", {})
    output_name = str(sub2api_cfg.get("output_file") or "sub2api_bundle.json").strip()
    output_file = run_dir / (output_name or "sub2api_bundle.json")
    return run_dir, accounts_file, output_file


def _configure_job_logging(job: PanelJob, level_name: str) -> JobLogHandler:
    handler = JobLogHandler(job)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    root.addHandler(handler)
    return handler


def _run_job(job: PanelJob, request: RunRequest) -> None:
    global _active_job, _last_job

    handler: JobLogHandler | None = None
    try:
        job.status = "running"
        config = load_config(request.config_path)
        _apply_threads_override(config, request.threads)
        if request.workspace_ids.strip():
            config.setdefault("workspace", {})["ids"] = _workspace_ids_list(request.workspace_ids)

        run_dir, accounts_file, output_file = _prepare_run_archive(config, request.count)
        handler = _configure_job_logging(job, str(config.get("logging", {}).get("level") or "INFO"))
        job.append_log(f"Job {job.id} started")
        if run_dir:
            job.append_log(f"Run directory: {run_dir}")

        summary = run_full_pipeline(
            config=config,
            count=request.count,
            output_file=str(output_file) if output_file else None,
            accounts_file=str(accounts_file) if accounts_file else None,
        )
        if run_dir:
            summary["run_dir"] = str(run_dir)
        job.summary = summary
        job.status = "succeeded" if int(summary.get("exported") or 0) > 0 else "failed"
        job.append_log(f"Job finished: {job.status}")
    except Exception as error:
        job.status = "failed"
        job.error = str(error)
        job.append_log(f"Job failed: {error}")
    finally:
        job.finished_at = time.time()
        if handler:
            logging.getLogger().removeHandler(handler)
        with _job_lock:
            _last_job = job
            _active_job = None


def _safe_file(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if PROJECT_ROOT not in resolved.parents and resolved != PROJECT_ROOT:
        raise HTTPException(status_code=403, detail="File is outside project directory")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config(path: str = "config.yaml") -> PanelConfig:
    config_path = _config_file(path)
    config = _read_yaml_config(config_path)
    return _panel_config_from_dict(config, config_path)


@app.post("/api/config")
def save_config(payload: PanelConfig) -> dict[str, str]:
    path = _config_file(payload.config_path)
    config = _dict_from_panel_config(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"status": "ok", "path": str(path)}


@app.post("/api/run")
def start_run(payload: RunRequest) -> dict[str, str]:
    global _active_job
    with _job_lock:
        if _active_job and _active_job.status in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="A job is already running")
        job = PanelJob(id=uuid.uuid4().hex[:12])
        _active_job = job
        thread = threading.Thread(target=_run_job, args=(job, payload), daemon=True)
        thread.start()
    return {"job_id": job.id, "status": job.status}


@app.get("/api/job")
def get_job() -> dict[str, Any]:
    job = _active_job or _last_job
    if not job:
        return {"status": "idle", "logs": [], "summary": {}}
    return {
        "id": job.id,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "summary": job.summary,
        "error": job.error,
        "logs": job.logs,
    }


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    config = load_config(DEFAULT_CONFIG_FILE)
    sub2api_cfg = config.get("sub2api", {})
    output_name = str(sub2api_cfg.get("output_file") or "sub2api_bundle.json").strip()
    output_name = output_name or "sub2api_bundle.json"
    output_cfg = config.get("output", {})
    runs_dir = Path(str(output_cfg.get("runs_dir") or "runs"))
    if not runs_dir.is_absolute():
        runs_dir = PROJECT_ROOT / runs_dir
    runs: list[dict[str, Any]] = []
    if runs_dir.exists():
        for item in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not item.is_dir():
                continue
            accounts_file = item / "registered_accounts.json"
            output_file = item / output_name
            accounts = load_accounts(accounts_file)
            runs.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "mtime": item.stat().st_mtime,
                    "accounts_count": len(accounts),
                    "accounts_file": str(accounts_file) if accounts_file.exists() else "",
                    "output_file": str(output_file) if output_file.exists() else "",
                }
            )
    return {"runs": runs[:50]}


@app.get("/api/download")
def download(path: str) -> FileResponse:
    file_path = _safe_file(path)
    return FileResponse(str(file_path), filename=file_path.name)


def open_browser_later(url: str) -> None:
    def _open() -> None:
        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()
