"""Единая карта доставки сайтов после подтверждённого Git-push."""

DEPLOY_PROFILES = {
    "mysite": {
        "remote": "origin",
        "production_branch": "main",
        "mode": "timeweb_autodeploy",
        "delivery": "Timeweb App Platform автоматически публикует push в main",
        "success": "push принят — Timeweb App Platform запустил автодеплой",
    },
    "demo2": {
        "remote": "origin",
        "production_branch": "main",
        "mode": "timeweb_cron",
        "delivery": "GitHub Actions собирает ветку deploy, затем Timeweb cron обновляет сайт",
        "success": "push принят — GitHub Actions собирает deploy, Timeweb cron заберёт сборку",
    },
    "demo3": {
        "remote": "origin",
        "production_branch": "main",
        "mode": "auto_deploy",
        "delivery": "GitHub Actions и Timeweb запускают автодеплой после push в main",
        "success": "push принят — автодеплой GitHub Actions/Timeweb запущен",
    },
    # Сайт остаётся monitoring-only для SEO, но его реальный production-контур
    # зафиксирован здесь, чтобы агент не терял инфраструктурное знание.
    "ai_mysite": {
        "remote": "origin",
        "production_branch": "main",
        "mode": "timeweb_autodeploy",
        "delivery": "Timeweb App Platform автоматически публикует push в main",
        "success": "push принят — Timeweb App Platform запустил автодеплой",
        "deploy_enabled": False,
    },
}


def deployment_profile(site: str) -> dict:
    """Возвращает копию профиля, чтобы вызывающий код не менял реестр."""
    return dict(DEPLOY_PROFILES.get(site, {}))
