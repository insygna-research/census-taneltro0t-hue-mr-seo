"""Безопасный мост Mr.Seo ↔ Codex для планов и правок сайтов.

По умолчанию Codex только читает выбранный репозиторий и выдаёт план. Явный
``--apply`` создаёт изолированный временный git worktree, проверяет build и
оставляет отдельную локальную ветку только при успешном результате. Основная
рабочая копия не переключается; сам bridge никогда не выполняет push/merge.
Для этого существует отдельный двухшаговый deploy-контур: build → merge →
push, после чего сайт забирает Timeweb autodeploy или Демо-бренд cron.

Запуск:
  venv/bin/python swarm/bridge.py mysite "усилить перелинковку блога" [--apply]
Отчёт: swarm/runs/bridge-<ts>.md + Telegram.
"""
from contextlib import contextmanager
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from carpathy.register import REPO_PATHS  # единый источник путей к репо
from secret_safety import redact_secrets

from swarm.brain import run as run_brain
from swarm.strategy import strategy_context
RUNS = ROOT / "swarm" / "runs"
LOCKS = ROOT / "swarm" / "locks"
TS = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"

GUARDRAILS = """
# Жёсткие рамки (нарушение = провал задачи)
- НЕ трогать: .env*, credentials, ключи, .git внутренности, node_modules.
- НЕ делать git push / merge / rebase — только правки файлов.
- Уважать уроки проекта: keywords в блоге low-light — ВСЕГДА массив (L-007);
  главную demo2 не переписывать; email канон mysiteconnect@gmail.com.
- Правки должны быть минимальными и по задаче, в стиле окружающего кода.
- В конце выведи короткий итог: какие файлы менял и зачем (или план, если read-only).
"""


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check).stdout.strip()


def _sensitive_paths(repo: str | Path) -> list[Path]:
    """Явные запреты для permission profile, без чтения содержимого."""
    base = Path(repo).resolve()
    denied = [
        base / ".git",
        base / "node_modules",
        base / ".next",
        base / ".vercel",
        base / "credentials",
        base / "secrets",
    ]
    denied.extend(p for p in base.glob(".env*"))
    # Монорепо иногда хранят .env на один уровень ниже.
    for child in base.iterdir():
        if child.is_dir() and child.name not in {".git", "node_modules", ".next"}:
            denied.extend(p for p in child.glob(".env*"))
    return list(dict.fromkeys(denied))


def run_ai_in_repo(repo: str, prompt: str, apply_mode: bool, timeout: int):
    denied = _sensitive_paths(repo)
    kwargs = (
        {"read_paths": (), "write_paths": (repo,), "deny_paths": denied}
        if apply_mode
        else {"read_paths": (repo,), "write_paths": (), "deny_paths": denied}
    )
    return run_brain(prompt, cwd=repo, timeout=timeout, **kwargs)[0]


def _run_build(repo: str, timeout: int = 300) -> tuple[int, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in {"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"}
    }
    env["NODE_ENV"] = "production"
    proc = subprocess.Popen(
        ["npm", "run", "build"], cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        return 124, f"build timeout ({timeout}s)"
    return proc.returncode, redact_secrets((stdout + stderr)[-1200:])


@contextmanager
def _site_lock(site: str):
    LOCKS.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS / f"bridge-{site}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _delete_branch_if_present(repo: str, branch: str) -> None:
    git(repo, "branch", "-D", branch, check=False)


def _apply_in_worktree(repo: str, site: str, task: str, base: str, report: list[str]) -> str:
    branch = f"mrseo/bridge-{TS}"
    dirty = git(repo, "status", "--porcelain")
    if dirty:
        report += ["## Отменено", "", "Основная рабочая копия содержит незакоммиченные изменения."]
        return f"🌉 Bridge·APPLY {site}: отменён — репозиторий содержит незакоммиченные изменения"

    temp_root = Path(tempfile.mkdtemp(prefix=f"mrseo-{site}-"))
    worktree = temp_root / "worktree"
    keep_branch = False
    added = False
    try:
        created = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            capture_output=True, text=True,
        )
        if created.returncode != 0:
            raise RuntimeError("git worktree add: " + redact_secrets(created.stderr)[-400:])
        added = True
        out = run_ai_in_repo(
            str(worktree),
            base + "\nРежим: ПРАВКИ РАЗРЕШЕНЫ в изолированной ветке. "
            "Внеси только минимальные изменения по задаче.",
            True,
            900,
        )
        report += ["## Отчёт Codex", "", out, ""]
        changed = git(str(worktree), "status", "--porcelain")
        if not changed:
            report += ["## Итог", "", "Codex не внёс изменений."]
            return f"🌉 Bridge·APPLY {site}: изменений не потребовалось"

        build_note = "build: пропущен (нет package.json)"
        if (worktree / "package.json").exists():
            rc, build_output = _run_build(str(worktree))
            if rc != 0:
                report += [
                    "## Проверка", "",
                    f"build: ❌ FAIL (код {rc})",
                    "```", build_output, "```",
                    "",
                    "Ветка удалена, изменения не сохранены.",
                ]
                return f"🌉 Bridge·APPLY {site}: отменён — build не прошёл"
            build_note = "build: ✅ OK"
        report += ["## Проверка", "", build_note, ""]

        git(str(worktree), "add", "-A")
        subject = " ".join(task.split())[:70]
        git(
            str(worktree), "commit", "-m",
            f"mrseo-bridge: {subject}\n\nCo-Authored-By: OpenAI Codex <noreply@openai.com>",
        )
        diff = git(str(worktree), "show", "--stat", "HEAD")
        keep_branch = True
        report += [
            "## Коммит (локальная ветка, БЕЗ push)", "",
            f"ветка `{branch}`", "```", diff[:1500], "```",
        ]
        return (
            f"🌉 Bridge·APPLY {site}: правки в ветке {branch}, build ОК; "
            "публикация — отдельной deploy-кнопкой"
        )
    finally:
        if added:
            subprocess.run(
                ["git", "-C", repo, "worktree", "remove", "--force", str(worktree)],
                capture_output=True, text=True,
            )
        if not keep_branch:
            _delete_branch_if_present(repo, branch)
        try:
            temp_root.rmdir()
        except OSError:
            pass


def notify(text: str):
    try:
        from telegram_notifier import send_long_message
        send_long_message(text)
    except Exception as e:
        print(f"[bridge] telegram: {e}")


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply_mode = "--apply" in sys.argv
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    site, task = args[0], args[1]
    repo = REPO_PATHS.get(site)
    if not repo or not Path(repo).exists():
        print(f"✗ неизвестный сайт или нет репо: {site}")
        sys.exit(1)

    task = task.strip()[:2000]
    base = (
        f"Ты работаешь в репозитории сайта ({site}). Задача от Mr.Seo:\n\n"
        f"{task}\n{GUARDRAILS}\n\n# ПОСТОЯННАЯ СТРАТЕГИЯ MR SEO\n\n"
        f"{strategy_context()}"
    )
    report = [
        f"# Bridge {TS} · {site} · {'APPLY' if apply_mode else 'PLAN'}",
        "", f"**Задача:** {task}", "",
    ]

    with _site_lock(site):
        try:
            if not apply_mode:
                out = run_ai_in_repo(
                    repo,
                    base + "\nРежим: ТОЛЬКО АНАЛИЗ. Изучи код и выдай "
                    "конкретный план правок (файлы, что менять, риски). "
                    "Ничего не редактируй.",
                    False,
                    600,
                )
                report += ["## План от Codex", "", out]
                verdict = f"🌉 Bridge·PLAN {site}: план готов"
            else:
                verdict = _apply_in_worktree(repo, site, task, base, report)
        except Exception as exc:
            error = redact_secrets(f"{type(exc).__name__}: {exc}")[:500]
            report += ["## Ошибка", "", error]
            verdict = f"🌉 Bridge {site}: ошибка, правки не применены"

    RUNS.mkdir(parents=True, exist_ok=True)
    p = RUNS / f"bridge-{TS}.md"
    p.write_text("\n".join(report), encoding="utf-8")
    notify(verdict + f"\n\nЗадача: {task[:150]}\nОтчёт: swarm/runs/{p.name}")
    print(verdict)
    print(f"отчёт: {p}")


if __name__ == "__main__":
    main()
