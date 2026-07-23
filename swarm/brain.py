"""Единый AI-бэкенд Mr.Seo на базе авторизованного Codex CLI.

Codex запускается с минимальным окружением, без пользовательских MCP/hooks и
без сети для команд. Для чувствительных сценариев можно передать явные
``read_paths``/``write_paths``: тогда beta permission profile ограничит даже
чтение файлов указанными каталогами.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterable

from secret_safety import redact_secrets


def _find_codex() -> str:
    configured = os.getenv("CODEX_BIN", "").strip()
    if configured and Path(configured).is_file() and os.access(configured, os.X_OK):
        return configured
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    candidates = [
        "/Applications/Codex.app/Contents/Resources/codex",
        *sorted(glob.glob(str(Path.home() / ".antigravity-ide/extensions/openai.chatgpt-*/bin/*/codex")),
                reverse=True),
        *sorted(glob.glob(str(Path.home() / ".vscode/extensions/openai.chatgpt-*/bin/*/codex")),
                reverse=True),
    ]
    found = next(
        (p for p in candidates if Path(p).is_file() and os.access(p, os.X_OK)),
        None,
    )
    if found:
        return found
    raise RuntimeError("Codex CLI не найден или не исполняемый")


CODEX_BIN = _find_codex()
MODEL = os.getenv("MRSEO_CODEX_MODEL", "").strip()
_ACTIVE_GROUPS: set[int] = set()
_ACTIVE_LOCK = threading.Lock()
_SIGNALS_READY = False


def _clean_env() -> dict[str, str]:
    """Окружение самого Codex: авторизация/locale есть, SEO-секретов нет."""
    allowed = {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG",
        "LC_ALL", "LC_CTYPE", "CODEX_HOME", "SSL_CERT_FILE",
        "CODEX_CA_CERTIFICATE",
    }
    env = {key: value for key, value in os.environ.items()
           if key in allowed and value}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env.setdefault("HOME", str(Path.home()))
    env["NO_COLOR"] = "1"
    return env


def _toml(value: str) -> str:
    """JSON-строка является корректной TOML basic string."""
    return json.dumps(value, ensure_ascii=False)


def _permission_config(
    read_paths: Iterable[str | Path],
    write_paths: Iterable[str | Path],
    deny_paths: Iterable[str | Path],
) -> list[str]:
    entries: dict[str, str] = {":minimal": "read"}
    for raw in read_paths:
        entries[str(Path(raw).expanduser().resolve())] = "read"
    for raw in write_paths:
        entries[str(Path(raw).expanduser().resolve())] = "write"
    for raw in deny_paths:
        # resolve(strict=False) сохраняет ещё не существующие, но явные пути.
        entries[str(Path(raw).expanduser().resolve(strict=False))] = "deny"
    table = "{ " + ", ".join(
        f"{_toml(path)}={_toml(access)}" for path, access in entries.items()
    ) + " }"
    return [
        "-c", 'default_permissions="mrseo-confined"',
        "-c", f"permissions.mrseo-confined.filesystem={table}",
        "-c", "permissions.mrseo-confined.network.enabled=false",
    ]


def _shell_policy(env: dict[str, str]) -> list[str]:
    forwarded = {
        key: env[key] for key in
        ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
        if env.get(key)
    }
    table = "{ " + ", ".join(
        f"{_toml(key)}={_toml(value)}" for key, value in forwarded.items()
    ) + " }"
    return [
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", f"shell_environment_policy.set={table}",
    ]


def _base_args(
    cwd: str | Path,
    sandbox: str,
    *,
    env: dict[str, str],
    read_paths: Iterable[str | Path] | None,
    write_paths: Iterable[str | Path] | None,
    deny_paths: Iterable[str | Path],
) -> list[str]:
    args = [CODEX_BIN, "-a", "never", "-C", str(Path(cwd).resolve())]
    if MODEL:
        args += ["-m", MODEL]
    if read_paths is not None or write_paths is not None:
        args += _permission_config(read_paths or (), write_paths or (), deny_paths)
    else:
        args += ["-s", sandbox]
    args += _shell_policy(env)
    args += [
        "-c", 'web_search="disabled"',
        "-c", "agents.enabled=false",
        "-c", "features.apps=false",
        "-c", "features.hooks=false",
        "-c", "features.goals=false",
        "-c", "allow_login_shell=false",
    ]
    return args


def _kill_group(proc: subprocess.Popen, *, force: bool = False) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _forward_shutdown(signum, _frame) -> None:
    """При остановке API/Python не оставлять отдельную группу Codex сиротой."""
    with _ACTIVE_LOCK:
        groups = tuple(_ACTIVE_GROUPS)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)


def _install_signal_handlers() -> None:
    global _SIGNALS_READY
    if _SIGNALS_READY or threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGTERM, _forward_shutdown)
    signal.signal(signal.SIGINT, _forward_shutdown)
    _SIGNALS_READY = True


def _safe_detail(text: str) -> str:
    return redact_secrets(text)[-800:]


def run(
    prompt: str,
    *,
    cwd: str | Path,
    timeout: int = 600,
    sandbox: str = "read-only",
    session_id: str | None = None,
    persistent: bool = False,
    read_paths: Iterable[str | Path] | None = None,
    write_paths: Iterable[str | Path] | None = None,
    deny_paths: Iterable[str | Path] = (),
    output_schema: str | Path | None = None,
) -> tuple[str, str | None]:
    """Запустить Codex.

    Если ``read_paths`` или ``write_paths`` переданы (даже пустым кортежем),
    старый широкий sandbox не используется: включается permission profile,
    где всё не перечисленное недоступно.
    """
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"рабочий каталог Codex не существует: {root}")
    if timeout <= 0:
        raise ValueError("timeout должен быть положительным")
    schema_path = None
    if output_schema is not None:
        schema_path = Path(output_schema).expanduser().resolve()
        if not schema_path.is_file():
            raise ValueError(f"JSON schema не найдена: {schema_path}")
    _install_signal_handlers()
    env = _clean_env()
    with tempfile.NamedTemporaryFile(prefix="mrseo-codex-", suffix=".txt", delete=False) as f:
        output_path = Path(f.name)
    proc: subprocess.Popen | None = None
    try:
        base = _base_args(
            root, sandbox, env=env, read_paths=read_paths,
            write_paths=write_paths, deny_paths=deny_paths,
        )
        if session_id:
            cmd = base + [
                "exec", "resume", "--ignore-user-config", "--ignore-rules",
            ]
            if schema_path:
                cmd += ["--output-schema", str(schema_path)]
            cmd += ["--json", "-o", str(output_path), session_id, "-"]
        else:
            cmd = base + ["exec", "--ignore-user-config", "--ignore-rules"]
            if not persistent:
                cmd.append("--ephemeral")
            if schema_path:
                cmd += ["--output-schema", str(schema_path)]
            cmd += ["--color", "never", "--json", "-o", str(output_path), "-"]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(root), env=env,
            start_new_session=True,
        )
        with _ACTIVE_LOCK:
            _ACTIVE_GROUPS.add(proc.pid)
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(proc, force=True)
                stdout, stderr = proc.communicate()
            raise TimeoutError(f"Codex не ответил за {timeout} секунд")
        text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        new_session = session_id
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("thread.started", "session.started"):
                new_session = event.get("thread_id") or event.get("session_id") or new_session
        if proc.returncode != 0 or not text:
            detail = _safe_detail(stderr or stdout or "нет ответа")
            raise RuntimeError(f"codex rc={proc.returncode}: {detail}")
        return text, new_session
    finally:
        if proc is not None:
            with _ACTIVE_LOCK:
                _ACTIVE_GROUPS.discard(proc.pid)
            if proc.poll() is None:
                _kill_group(proc, force=True)
                proc.wait(timeout=3)
        output_path.unlink(missing_ok=True)
