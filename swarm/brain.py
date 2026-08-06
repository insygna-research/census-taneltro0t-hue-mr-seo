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


def _find_claude() -> str | None:
    configured = os.getenv("CLAUDE_BIN", "").strip()
    if configured and Path(configured).is_file() and os.access(configured, os.X_OK):
        return configured
    return shutil.which("claude") or next(
        (p for p in (
            str(Path.home() / ".npm-global/bin/claude"),
            str(Path.home() / ".claude/local/claude"),
            "/usr/local/bin/claude", "/opt/homebrew/bin/claude",
        ) if Path(p).is_file() and os.access(p, os.X_OK)),
        None,
    )


# Двухмозговая схема: думающие роли (ассистент/аналитик/чат — полностью
# конфайнутые вызовы без доступа к файлам) идут на Claude Opus по Max-подписке,
# правки кода (bridge) и всё с write-доступом остаются на Codex. Два пула
# лимитов вместо одного; при ошибке Claude свежий вызов тихо падает на Codex.
CLAUDE_BIN = _find_claude()
CLAUDE_MODEL = os.getenv("MRSEO_CLAUDE_MODEL", "opus").strip()
BRAIN_PRIMARY = os.getenv("MRSEO_BRAIN", "claude").strip()  # claude | codex
_CLAUDE_SID = "claude:"
# нейтральный пустой cwd: в headless-режиме чтение вне cwd авто-запрещено,
# так Claude физически не дотягивается до credentials/ и снапшотов
_CLAUDE_HOME = Path(__file__).resolve().parent / ".brain_home"
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


def _run_claude(
    prompt: str,
    *,
    timeout: int,
    session_id: str | None,
) -> tuple[str, str | None]:
    """Headless `claude -p` (Opus по Max-подписке) для конфайнутых ролей.

    Без инструментов: Bash/Write/Edit/Web запрещены явно, чтение вне пустого
    cwd в -p режиме авто-запрещается. Сессии неймспейсятся «claude:<id>»,
    старые codex-треды продолжают ходить в Codex.
    """
    _CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    env = _clean_env()
    cmd = [
        CLAUDE_BIN, "-p", "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--strict-mcp-config",
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task,Agent,TodoWrite",
    ]
    if session_id:
        cmd += ["--resume", session_id[len(_CLAUDE_SID):]]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=str(_CLAUDE_HOME), env=env,
        start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_GROUPS.add(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(proc, force=True)
                stdout, stderr = proc.communicate()
            raise TimeoutError(f"Claude не ответил за {timeout} секунд")
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        text = (payload.get("result") or "").strip()
        if proc.returncode != 0 or payload.get("is_error") or not text:
            raise RuntimeError(
                f"claude rc={proc.returncode}: "
                f"{_safe_detail(text or stderr or stdout or 'нет ответа')}"
            )
        sid = payload.get("session_id")
        return text, (_CLAUDE_SID + sid) if sid else session_id
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_GROUPS.discard(proc.pid)
        _kill_group(proc, force=True)


def _run_claude_repo(
    prompt: str,
    *,
    root: Path,
    timeout: int,
    write: bool,
) -> tuple[str, str | None]:
    """Opus в репозитории: план (read-only) или правки (acceptEdits в worktree).

    Секреты закрыты deny-правилами на Read; Bash/сеть запрещены всегда —
    build-проверку делает сам bridge отдельным npm-процессом.
    """
    env = _clean_env()
    deny = [
        "Read(**/.env*)", "Read(**/credentials/**)", "Read(**/secrets/**)",
        "Read(**/.git/**)", "Read(**/node_modules/**)",
        "Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit",
    ]
    if not write:
        deny += ["Write", "Edit", "MultiEdit"]
    settings = json.dumps({"permissions": {"deny": deny}})
    cmd = [
        CLAUDE_BIN, "-p", "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--strict-mcp-config",
        "--settings", settings,
    ]
    if write:
        cmd += ["--permission-mode", "acceptEdits"]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=str(root), env=env,
        start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_GROUPS.add(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(proc, force=True)
                stdout, stderr = proc.communicate()
            raise TimeoutError(f"Claude не ответил за {timeout} секунд")
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        text = (payload.get("result") or "").strip()
        if proc.returncode != 0 or payload.get("is_error") or not text:
            raise RuntimeError(
                f"claude-repo rc={proc.returncode}: "
                f"{_safe_detail(text or stderr or stdout or 'нет ответа')}"
            )
        return text, None
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_GROUPS.discard(proc.pid)
        _kill_group(proc, force=True)


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
    if read_paths is not None:
        read_paths = tuple(read_paths)
    if write_paths is not None:
        write_paths = tuple(write_paths)
    # Маршрутизация мозга: Claude Opus берёт только полностью конфайнутые
    # вызовы (read_paths=() и write_paths=() — ассистент/аналитик/чат без
    # файлового доступа); всё остальное (bridge, схемы) — Codex как раньше.
    confined = read_paths == () and write_paths == ()
    is_claude_session = bool(session_id) and session_id.startswith(_CLAUDE_SID)
    if (
        BRAIN_PRIMARY == "claude" and CLAUDE_BIN and confined
        and output_schema is None
        and (session_id is None or is_claude_session)
    ):
        try:
            return _run_claude(prompt, timeout=timeout, session_id=session_id)
        except Exception:
            if is_claude_session:
                raise  # claude-тред нельзя продолжить в codex
            # свежий вызов: тихий фолбэк на codex, рой не умирает на лимитах
    elif is_claude_session:
        raise RuntimeError("claude-сессия, но Claude-мозг недоступен/выключен")
    # Мост-режим: Opus работает прямо в репо (план или правки в worktree),
    # если репо-доступ задан явно. Фолбэк — Codex как раньше.
    repo_scoped = (
        (read_paths and len(read_paths) == 1) if write_paths == () else
        (write_paths and len(write_paths) == 1 and read_paths == ())
    )
    if (
        os.getenv("MRSEO_BRIDGE_BRAIN", "claude").strip() == "claude"
        and CLAUDE_BIN and repo_scoped and output_schema is None
        and session_id is None
    ):
        try:
            return _run_claude_repo(
                prompt, root=root, timeout=timeout,
                write=bool(write_paths),
            )
        except Exception:
            pass  # фолбэк на codex ниже
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
