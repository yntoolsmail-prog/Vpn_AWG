import json, logging, subprocess, os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from awg_core import (
    ADMIN_ID, AWG_CONF, BOT_SERVICE, SERVER_PORT, TMA_URL,
    get_all_clients, is_approved, load_servers, load_users,
)
from .common import IMG_BASE, back_kb, BTN_BACK_MENU, BTN_CANCEL

logger = logging.getLogger(__name__)

REPO_OWNER  = "yntoolsmail-prog"
REPO_NAME   = "Vpn_AWG"
REPO_BRANCH = "main"
REPO_URL    = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
REPO_COMMIT_FILE = "/etc/amnezia/amneziawg/last_commit.txt"


def _gh_api(path: str) -> dict | None:
    """GET к GitHub API, возвращает dict или None при ошибке."""
    try:
        out = subprocess.check_output(
            ["curl", "-s", "--max-time", "10",
             "-H", "Accept: application/vnd.github+json",
             f"https://api.github.com/{path}"],
            text=True,
        )
        return json.loads(out)
    except Exception:
        return None

def _read_last_commit() -> str:
    """Читает сохранённый SHA последнего известного коммита."""
    try:
        with open(REPO_COMMIT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def _write_last_commit(sha: str):
    """Сохраняет SHA коммита на диск."""
    os.makedirs(os.path.dirname(REPO_COMMIT_FILE), exist_ok=True)
    with open(REPO_COMMIT_FILE, "w") as f:
        f.write(sha)

async def check_repo_updates(context: ContextTypes.DEFAULT_TYPE):
    """Job: проверяет новые коммиты в репозитории каждые 30 минут.
    При появлении изменений отправляет уведомление админу."""
    data = _gh_api(f"repos/{REPO_OWNER}/{REPO_NAME}/commits/{REPO_BRANCH}")
    if not data or "sha" not in data:
        logger.warning("check_repo_updates: не удалось получить данные из GitHub API")
        return

    latest_sha  = data["sha"]
    short_sha   = latest_sha[:7]
    last_known  = _read_last_commit()

    if not last_known:
        # Первый запуск — просто запоминаем текущий коммит, не шумим
        _write_last_commit(latest_sha)
        logger.info(f"check_repo_updates: инициализация, текущий коммит {short_sha}")
        return

    if latest_sha == last_known:
        logger.info(f"check_repo_updates: изменений нет ({short_sha})")
        return

    # Есть новый коммит — собираем данные
    commit_info  = data.get("commit", {})
    message      = commit_info.get("message", "—").split("\n")[0][:80]
    author       = commit_info.get("author", {}).get("name", "—")
    date_raw     = commit_info.get("author", {}).get("date", "")
    date_str     = date_raw[:10] if date_raw else "—"
    compare_url  = f"{REPO_URL}/compare/{last_known[:7]}...{short_sha}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Смотреть изменения", url=compare_url)],
        [InlineKeyboardButton("🔄 Обновить сейчас",    callback_data=f"repo_update_{latest_sha}")],
        [InlineKeyboardButton("⏭ Пропустить",          callback_data=f"repo_skip_{latest_sha}")],
    ])

    text = (
        f"🆕 *Новые изменения в репозитории!*\n\n"
        f"📦 [`{short_sha}`]({REPO_URL}/commit/{latest_sha})\n"
        f"✏️ {message}\n"
        f"👤 {author}  •  {date_str}\n\n"
        f"👆 Нажмите «Смотреть изменения» или перейдите на сервер\n"
        f"и выполните обновление через `vpn.sh` → Обновление."
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    logger.info(f"check_repo_updates: новый коммит {short_sha}, уведомление отправлено")

async def do_repo_update(query, sha: str):
    """Обновляет все файлы проекта через setup.sh --update и перезапускает бота."""
    short_sha = sha[:7]
    await query.edit_message_text(
        f"⏳ Обновляю все файлы проекта (коммит `{short_sha}`)...",
        parse_mode="Markdown",
    )
    try:
        result = subprocess.run(
            ["bash", "/root/setup.sh", "--update"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "setup.sh --update вернул ненулевой код")

        _write_last_commit(sha)
        await query.edit_message_text(
            f"✅ *Обновление выполнено!*\n\n"
            f"📦 Коммит: `{short_sha}`\n"
            f"📄 Все файлы проекта обновлены.\n\n"
            f"⏳ Бот перезапускается...",
            parse_mode="Markdown",
        )
        subprocess.Popen(["bash", "-c", f"sleep 2 && systemctl restart {BOT_SERVICE}"])

    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка при обновлении: `{e}`\n\n"
            f"Зайдите на сервер: `bash /root/setup.sh --update`",
            parse_mode="Markdown",
        )

async def send_start_hello(context: ContextTypes.DEFAULT_TYPE):
    """Job: запускается через 5 секунд после старта бота.
    Шлёт сообщение ТОЛЬКО если есть флаг-файл с chat_id пользователя который нажал кнопку.
    При автоматическом рестарте systemd флага нет — молчим, не спамим."""
    from awg_core import RESTART_FLAG_FILE
    if not os.path.exists(RESTART_FLAG_FILE):
        return  # автоматический рестарт — не беспокоим

    try:
        with open(RESTART_FLAG_FILE) as f:
            chat_id = int(f.read().strip())
        os.remove(RESTART_FLAG_FILE)
    except Exception:
        try:
            os.remove(RESTART_FLAG_FILE)
        except Exception:
            pass
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Открыть меню", callback_data="back")],
    ])
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ Бот перезапущен и готов к работе.",
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"send_start_hello: не удалось отправить сообщение: {e}")

