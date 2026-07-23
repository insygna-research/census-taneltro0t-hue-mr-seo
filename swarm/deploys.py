"""
swarm/deploys — подтверждённый Git-конвейер публикации Mr.Seo.

Bridge только готовит локальную ветку. Эта команда вызывается отдельной
двухшаговой кнопкой в dashboard и выполняет:

  точный SHA → актуальный main → merge → повторный build → push origin/main.

После push Low Light/AI забирает Timeweb App Platform, Демо-бренд проходит через
GitHub Actions и серверный cron, «Основа» — через свой автодеплой.

Команды (stdout=JSON):
  list
  merge <site> <branch> <expected_sha>
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from carpathy.register import REPO_PATHS
from secret_safety import redact_secrets
from swarm.deploy_config import deployment_profile

HYP = ROOT / "carpathy" / "hypotheses.json"
RUNS = ROOT / "swarm" / "runs"
LOCKS = ROOT / "swarm" / "locks"


def _git(repo, *args, timeout=60):
    command = ["git", "-C", repo, *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=stdout,
            stderr=stderr or f"git timeout ({timeout}s)",
        )


def _git_text(repo, *args) -> str:
    result = _git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 else ""


@contextmanager
def _site_lock(site: str):
    LOCKS.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS / f"deploy-{site}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_build(repo: str, timeout: int = 300) -> tuple[int, str]:
    if not (Path(repo) / "package.json").exists():
        return 0, "build пропущен: package.json отсутствует"
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"}
    }
    env["NODE_ENV"] = "production"
    proc = subprocess.Popen(
        ["npm", "run", "build"],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
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


def _branch_sha(repo: str, branch: str) -> str:
    result = _git(repo, "show-ref", "--verify", "--hash", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_sha(repo: str, remote: str, branch: str) -> str:
    result = _git(repo, "ls-remote", remote, f"refs/heads/{branch}", timeout=90)
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


def _overlapping_files(repo: str, base_ref: str, branch: str) -> list[str]:
    """Файлы, которые менялись и в production после fork, и в ветке."""
    base = _git_text(repo, "merge-base", base_ref, branch)
    if not base:
        return []
    main_changes = set(
        line
        for line in _git_text(repo, "diff", "--name-only", f"{base}..{base_ref}").splitlines()
        if line
    )
    branch_changes = set(
        line
        for line in _git_text(repo, "diff", "--name-only", f"{base}..{branch}").splitlines()
        if line
    )
    return sorted(main_changes & branch_changes)


def _restore(
    repo: str,
    old_main: str,
    previous_branch: str,
    production_branch: str,
) -> None:
    """Откатывает только merge, созданный текущей подтверждённой операцией."""
    _git(repo, "merge", "--abort")
    current = _git_text(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current == production_branch and old_main:
        _git(repo, "reset", "--hard", old_main)
    if previous_branch and previous_branch != production_branch:
        _git(repo, "checkout", previous_branch)


def _task_for_branch(branch: str) -> str:
    """Достаём текст задачи из отчёта моста по таймстампу ветки."""
    m = re.search(r"bridge-(\d{8}-\d{4})", branch)
    if not m:
        return ""
    f = RUNS / f"bridge-{m.group(1)}.md"
    if not f.exists():
        return ""
    for ln in f.read_text(encoding="utf-8").splitlines():
        if ln.startswith("**Задача:**"):
            return ln.replace("**Задача:**", "").strip()[:160]
    return ""


def list_deploys() -> dict:
    pending, merged = [], []
    # 1) ожидающие: только ветки с реальными коммитами сверх production main.
    for site, repo in REPO_PATHS.items():
        if not Path(repo or "").exists():
            continue
        profile = deployment_profile(site)
        if not profile or profile.get("deploy_enabled") is False:
            continue
        production_branch = profile["production_branch"]
        remote = profile["remote"]
        fetched = _git(repo, "fetch", remote, production_branch, timeout=20)
        fetch_error = fetched.returncode != 0
        production_ref = (
            f"{remote}/{production_branch}"
            if _git_text(repo, "rev-parse", f"{remote}/{production_branch}")
            else production_branch
        )
        local_main = _git_text(repo, "rev-parse", production_branch)
        remote_main = _git_text(repo, "rev-parse", production_ref)
        dirty = bool(_git(repo, "status", "--porcelain").stdout.strip())
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/mrseo/",
        )
        if refs.returncode != 0:
            continue
        for branch in [line.strip() for line in refs.stdout.splitlines() if line.strip()]:
            ahead = _git(repo, "rev-list", "--count", f"{production_ref}..{branch}")
            if ahead.returncode != 0 or int(ahead.stdout.strip() or "0") <= 0:
                continue
            full_sha = _branch_sha(repo, branch)
            info = _git(repo, "log", "-1", "--format=%cs|%h", branch).stdout.strip()
            date, short_sha = (info.split("|") + [""])[:2]
            overlap = _overlapping_files(repo, production_ref, branch)
            blocked_reason = ""
            if fetch_error:
                blocked_reason = "не удалось обновить origin/main — публикация временно заблокирована"
            elif dirty:
                blocked_reason = "репозиторий содержит незакоммиченные изменения"
            elif local_main != remote_main:
                blocked_reason = "локальный main не синхронизирован с origin/main"
            elif overlap:
                preview = ", ".join(overlap[:3])
                blocked_reason = (
                    "main изменился в тех же файлах после создания ветки"
                    + (f": {preview}" if preview else "")
                    + " — ветку нужно пересобрать"
                )
            pending.append(
                {
                    "site": site,
                    "branch": branch,
                    "date": date,
                    "sha": short_sha,
                    "expected_sha": full_sha,
                    "task": _task_for_branch(branch),
                    "stage": "awaiting_merge",
                    "delivery": profile["delivery"],
                    "deployment_mode": profile["mode"],
                    "blocked_reason": blocked_reason or None,
                }
            )
    # 2) смерженные: гипотезы от merge_watch / mrseo-bridge коммитов
    try:
        hyps = json.loads(HYP.read_text(encoding="utf-8"))["hypotheses"]
    except Exception:
        hyps = []
    for h in hyps:
        ch = str(h.get("change", ""))
        by = str(h.get("registered_by", ""))
        if (
            "merge_watch" not in by
            and "swarm/deploys.py" not in by
            and "mrseo-bridge" not in ch
        ):
            continue
        status = h.get("status", "pending")
        stage = {"pending": "verifying", "observe": "verifying",
                 "confirmed": "confirmed", "partial": "partial", "falsified": "falsified"}.get(status, "verifying")
        task = ch.replace("[авто/merge моста] ", "").replace("[авто/deploy] ", "")
        merged.append({"site": h.get("site", ""), "id": h["id"], "sha": h.get("commit"),
                       "date": h.get("commit_date"), "task": task[:160],
                       "stage": stage, "verify_due": h.get("verify_due"),
                       "verdict": h.get("actual_effect", "")[:200] if status in ("confirmed", "partial", "falsified") else None,
                       "targets": h.get("targets_moved", [])})
    merged.sort(key=lambda x: x.get("date") or "", reverse=True)
    pending.sort(key=lambda item: (item.get("date") or "", item["site"]), reverse=True)
    return {"pending": pending, "merged": merged[:30]}


def _post_deploy(
    site: str,
    branch: str,
    merge_sha: str,
    source_sha: str,
    deployment_mode: str,
    success_note: str,
) -> str:
    """Регистрирует эксперимент и уведомляет только после подтверждённого push."""
    note = ""
    try:
        from swarm.merge_watch import register_deploy

        hypothesis = register_deploy(
            site,
            merge_sha,
            source_sha,
            branch,
            deployment_mode,
        )
        note = f"; гипотеза: {hypothesis}" if hypothesis else ""
    except Exception:
        pass
    try:
        from telegram_notifier import send_long_message

        send_long_message(
            f"🚀 Деплой {site}: {branch} слит и запушен. {success_note}{note}"
        )
    except Exception:
        pass
    return note


def merge(site: str, branch: str, expected_sha: str) -> dict:
    repo = REPO_PATHS.get(site)
    if not repo or not Path(repo).exists():
        return {"ok": False, "error": f"нет репо для {site}"}
    profile = deployment_profile(site)
    if not profile or profile.get("deploy_enabled") is False:
        return {"ok": False, "error": f"deploy для {site} отключён"}
    if not branch.startswith("mrseo/"):
        return {"ok": False, "error": "разрешены только ветки mrseo/*"}
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected_sha or ""):
        return {"ok": False, "error": "нужен точный SHA подтверждённой ветки"}

    remote = profile["remote"]
    production_branch = profile["production_branch"]
    with _site_lock(site):
        if _git(repo, "status", "--porcelain").stdout.strip():
            return {
                "ok": False,
                "error": "в репо незакоммиченные правки — deploy остановлен",
            }

        actual_sha = _branch_sha(repo, branch)
        if not actual_sha:
            return {"ok": False, "error": f"ветки {branch} нет"}
        if actual_sha != expected_sha:
            return {
                "ok": False,
                "error": "ветка изменилась после показа карточки — обновите страницу и проверьте diff",
            }

        fetched = _git(repo, "fetch", remote, production_branch, timeout=120)
        if fetched.returncode != 0:
            return {
                "ok": False,
                "error": "не удалось проверить origin/main: "
                + redact_secrets(fetched.stderr or fetched.stdout)[-180:],
            }

        local_main = _git_text(repo, "rev-parse", production_branch)
        remote_main = _git_text(repo, "rev-parse", f"{remote}/{production_branch}")
        if not local_main or not remote_main or local_main != remote_main:
            return {
                "ok": False,
                "error": "локальный main не совпадает с origin/main — сначала синхронизируйте репозиторий",
            }

        ahead = _git(repo, "rev-list", "--count", f"{production_branch}..{branch}")
        if ahead.returncode != 0 or int(ahead.stdout.strip() or "0") <= 0:
            return {"ok": False, "error": "ветка уже слита или не содержит новых коммитов"}
        overlap = _overlapping_files(repo, production_branch, branch)
        if overlap:
            return {
                "ok": False,
                "error": "main после создания ветки менял те же файлы ("
                + ", ".join(overlap[:4])
                + ") — пересоберите ветку на актуальном main",
            }

        previous_branch = _git_text(repo, "rev-parse", "--abbrev-ref", "HEAD")
        checkout = _git(repo, "checkout", production_branch)
        if checkout.returncode != 0:
            return {
                "ok": False,
                "error": "не удалось открыть main: "
                + redact_secrets(checkout.stderr or checkout.stdout)[-180:],
            }

        merged = _git(
            repo,
            "merge",
            "--no-ff",
            branch,
            "-m",
            f"merge {branch} [Mr.Seo deploy]",
            timeout=120,
        )
        if merged.returncode != 0:
            _restore(repo, local_main, previous_branch, production_branch)
            return {
                "ok": False,
                "error": "конфликт merge — нужен человек: "
                + redact_secrets(merged.stdout or merged.stderr)[-180:],
            }

        merge_sha = _git_text(repo, "rev-parse", "HEAD")
        build_code, build_output = _run_build(repo)
        if build_code != 0:
            _restore(repo, local_main, previous_branch, production_branch)
            return {
                "ok": False,
                "error": f"build не прошёл (код {build_code}): {build_output[-240:]}",
            }
        if _git(repo, "status", "--porcelain").stdout.strip():
            _restore(repo, local_main, previous_branch, production_branch)
            return {
                "ok": False,
                "error": "build изменил рабочие файлы — ветку нужно пересобрать и закоммитить результат",
            }

        pushed = _git(
            repo,
            "push",
            remote,
            f"{merge_sha}:refs/heads/{production_branch}",
            timeout=180,
        )
        if pushed.returncode != 0:
            # Сетевой разрыв мог случиться после приёма push. Сначала сверяем
            # remote SHA и только затем решаем, откатывать ли локальный merge.
            accepted_sha = _remote_sha(repo, remote, production_branch)
            if accepted_sha != merge_sha:
                _restore(repo, local_main, previous_branch, production_branch)
                return {
                    "ok": False,
                    "error": "push не прошёл, локальный merge отменён: "
                    + redact_secrets(pushed.stderr or pushed.stdout)[-180:],
                }

        branch_cleanup = _git(repo, "branch", "-d", branch)
        restore_note = ""
        if previous_branch not in {"", "HEAD", production_branch, branch}:
            restored = _git(repo, "checkout", previous_branch)
            if restored.returncode != 0:
                restore_note = "; рабочая копия осталась на main"
        cleanup_note = (
            ""
            if branch_cleanup.returncode == 0
            else "; локальная ветка сохранена (возможно, открыта в worktree)"
        )
        hypothesis_note = _post_deploy(
            site,
            branch,
            merge_sha,
            actual_sha,
            profile["mode"],
            profile["success"],
        )
        return {
            "ok": True,
            "note": profile["success"] + hypothesis_note + cleanup_note + restore_note,
            "deployment_mode": profile["mode"],
            "merge_sha": merge_sha,
        }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        print(json.dumps(list_deploys(), ensure_ascii=False))
    elif cmd == "merge":
        if len(sys.argv) != 5:
            print(json.dumps({"ok": False, "error": "merge <site> <branch> <expected_sha>"}, ensure_ascii=False))
        else:
            print(json.dumps(merge(sys.argv[2], sys.argv[3], sys.argv[4]), ensure_ascii=False))
    else:
        print(json.dumps({"error": "list | merge <site> <branch> <expected_sha>"}))
