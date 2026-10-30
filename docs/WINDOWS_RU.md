# Запуск Discord Social Bot на Windows

[← Русский README](../README.ru.md) · [Подключение к Discord](DISCORD_SETUP_RU.md) · [VPS / Ubuntu](VPS_RU.md)

Подойдёт для локальной работы и тестирования. **Бот работает только пока запущен процесс Python и включён компьютер.** Для постоянной работы 24/7 лучше VPS.

## 1. Подготовь инструменты

- Установи **Python 3.13** с [python.org](https://www.python.org/downloads/). При установке можно включить добавление Python в PATH, но в командах ниже используется `py -3.13`.
- Установи **Git** с [git-scm.com](https://git-scm.com/downloads/win).
- Установи Windows-сборку **FFmpeg** со страницы [ffmpeg.org](https://ffmpeg.org/download.html), добавь папку с `ffmpeg.exe` и `ffprobe.exe` в `PATH`. Новую консоль открой после изменения `PATH`.

Проверь в PowerShell:

```powershell
py -3.13 --version
git --version
ffmpeg -version
ffprobe -version
```

Если `py` не распознаётся, проверь установку Python / Python Launcher. Если `ffmpeg` не найден, проверь `PATH` и открой PowerShell заново.

## 2. Скачай исходники и установи зависимости

В PowerShell (в удобной рабочей папке, например `Documents`):

```powershell
cd "$env:USERPROFILE\Documents"
git clone https://github.com/Vetal12350/Discord-Social-Bot.git
cd Discord-Social-Bot
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` укажи токен из [Discord Developer Portal](DISCORD_SETUP_RU.md):

```dotenv
DISCORD_TOKEN=PASTE_YOUR_TOKEN_HERE
REPOST_MODE=webhook
TRANSLATE_ENABLED=true
```

Редактируй файл локально и не прикладывай его к публичным Issues/скриншотам.

## 3. Проверка и запуск

```powershell
.\.venv\Scripts\python.exe check_runtime.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe bot.py
```

Проверь сообщения `Connected to Discord` и `Slash commands synced` (либо предупреждение о временной проблеме синхронизации). Отправь публичную ссылку в тестовый канал.

Для остановки нажми **Ctrl+C** в окне PowerShell. В отличие от VPS, автоматический перезапуск здесь не настроен.

## Обновление

Останови бота через Ctrl+C, затем из папки проекта:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe check_runtime.py
```

После этого запускай `bot.py` снова. Приватный `.env` не добавляй в Git.

Если установка или работа медиа не удалась, смотри [частые ошибки](TROUBLESHOOTING_RU.md).
