# Установка Discord Social Bot на Ubuntu VPS (24/7)

[← Русский README](../README.ru.md) · [Создать бота в Discord](DISCORD_SETUP_RU.md) · [Частые ошибки](TROUBLESHOOTING_RU.md)

Пошаговая установка на **Ubuntu 24.04** с отдельным системным пользователем и автозапуском через `systemd`. Схема проверена при развёртывании на VPS. Указанные команды не затрагивают другие службы и проекты на том же сервере, если запускать их именно в описанных каталогах.

> Для Linux VPS нужен доступ по SSH и возможность выполнять `sudo`. На хостингах вроде Altare/Pterodactyl без SSH и systemd используй [ALTARE.md](../ALTARE.md). Команды `sudo` вводятся **на VPS**, а не в Windows PowerShell.

## 0. Подготовь Discord

До установки создай бота в [Developer Portal](https://discord.com/developers/applications), включи **Message Content Intent**, пригласи на сервер со scope `bot` и `applications.commands`. [Полная инструкция](DISCORD_SETUP_RU.md).

Токен держи в секрете. Если бот уже запущен на другом хостинге, **не запускай вторую копию до остановки старой**.

## 1. Проверь Ubuntu и установи пакеты

Подключись к серверу по SSH и выполни:

```bash
cat /etc/os-release | head -6
python3 --version
sudo apt update
sudo apt install -y git ffmpeg python3-venv python3-pip
ffmpeg -version | head -1
ffprobe -version | head -1
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'libwebp_anim|[[:space:]]gif[[:space:]]'
```

На Ubuntu 24.04 системный Python обычно **3.12**; эта версия успешно проходила проверку импорта и тестов при нашем развёртывании. Для новых установок подойдёт также **Python 3.13/3.14** при наличии совместимых зависимостей. Не заменяй системный Python Ubuntu вручную.

Наличие `ffmpeg` / `ffprobe` важно для обработки анимаций; обычные совместимые видео и изображения не всегда требуют перекодирования.

## 2. Отдельный пользователь и каталог

Создаём пользователя **без интерактивного входа**, чтобы код не работал от root:

```bash
sudo adduser --system --group \
  --home /opt/discord-social-bot \
  --shell /usr/sbin/nologin socialbot

sudo -u socialbot git clone --depth 1 \
  https://github.com/Vetal12350/Discord-Social-Bot.git \
  /opt/discord-social-bot/app

sudo -u socialbot python3 -m venv /opt/discord-social-bot/venv

sudo -u socialbot /opt/discord-social-bot/venv/bin/python \
  -m pip install -r /opt/discord-social-bot/app/requirements.txt
```

Если пользователь или каталог уже существует, не повторяй создание вслепую: сначала проверь `id socialbot` и `ls -ld /opt/discord-social-bot`. Не удаляй существующие файлы и не перезаписывай чужие директории.

Исходный код лежит в `/opt/discord-social-bot/app`, а зависимости — в отдельном `/opt/discord-social-bot/venv`. Это не конфликтует с виртуальными окружениями других ботов.

## 3. Приватные настройки

Создай `.env` в каталоге проекта, с правами доступа только для пользователя бота:

```bash
sudo install -o socialbot -g socialbot -m 600 /dev/null \
  /opt/discord-social-bot/app/.env
sudo nano /opt/discord-social-bot/app/.env
```

Вставь (с настоящим токеном вместо заглушки):

```dotenv
DISCORD_TOKEN=PASTE_YOUR_TOKEN_HERE
REPOST_MODE=webhook
TRANSLATE_ENABLED=true
TEST_GUILD_ID=
ALLOWED_CHANNEL_IDS=
MYMEMORY_EMAIL=
```

Чтобы сохранить в nano: **Ctrl+O**, Enter, **Ctrl+X**. Затем убедись в правах, не выводя содержимое файла:

```bash
sudo chown socialbot:socialbot /opt/discord-social-bot/app/.env
sudo chmod 600 /opt/discord-social-bot/app/.env
sudo stat -c '%U:%G %a %n' /opt/discord-social-bot/app/.env
```

Не записывай токен непосредственно в команду терминала, не отправляй `.env` в GitHub и не прикладывай его скриншоты. Дополнительные параметры объяснены в [русском README](../README.ru.md).

## 4. Проверка **без подключения к Discord**

```bash
sudo -u socialbot /opt/discord-social-bot/venv/bin/python \
  /opt/discord-social-bot/app/check_runtime.py

sudo -u socialbot sh -c '
  cd /opt/discord-social-bot/app &&
  ../venv/bin/python -m unittest discover -s tests -v
'
```

В норме появится `Bot imports and Pillow WebP: OK`, для совместимого FFmpeg — `GIF preflight: OK`, а тесты завершатся `OK`.

Проверка `check_runtime.py` **не использует токен** и не запускает основной процесс. Она не доказывает, что внешние X/TikTok и Discord доступны — их проверяем позже в тестовом канале.

## 5. Создание службы systemd

Создай файл службы:

```bash
sudo tee /etc/systemd/system/discord-social-bot.service > /dev/null <<'EOF'
[Unit]
Description=Discord Social Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=socialbot
Group=socialbot
WorkingDirectory=/opt/discord-social-bot/app
ExecStart=/opt/discord-social-bot/venv/bin/python -u bot.py
Restart=on-failure
RestartSec=10

# Пример лимитов для VPS с несколькими службами;
# меняй их только после оценки нагрузки.
MemoryHigh=768M
MemoryMax=1200M
CPUQuota=100%
OOMScoreAdjust=500

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemd-analyze verify discord-social-bot.service
systemctl is-active discord-social-bot.service
```

Ожидаемый статус до первого запуска — `inactive`. Отсутствие вывода `systemd-analyze verify` обычно означает, что синтаксических ошибок нет.

**Лимиты здесь — пример для небольшого VPS**, а не гарантия производительности. `CPUQuota=100%` ограничивает службу примерно одним логическим ядром; `MemoryMax=1200M` может завершить её при превышении. Если установлена тяжёлая обработка медиа, анализируй нагрузку и журнал.

## 6. Запуск и тестирование

**Если бот с тем же токеном работает на другом хостинге, сначала останови старый процесс**, проверь его статус, только затем запускай новый:

```bash
sudo systemctl enable --now discord-social-bot.service
systemctl is-active discord-social-bot.service
sudo systemctl status discord-social-bot.service --no-pager -l
sudo journalctl -u discord-social-bot.service -n 50 --no-pager
```

В журнале ищи `Connected to Discord` и `Slash commands synced`. Далее в закрытом тестовом канале проверь X/Twitter, GIF, TikTok, `/video`, оформление вебхука и правило удаления одной ссылки.

**Не включай новый и старый экземпляры одновременно**. Простой `active` не означает, что все внешние медиа работают: тестируй реальные сообщения.

## 7. Управление и обновление

```bash
# Состояние и последние логи
sudo systemctl status discord-social-bot.service --no-pager
sudo journalctl -u discord-social-bot.service -n 100 --no-pager

# Перезапуск после изменения настроек
sudo systemctl restart discord-social-bot.service

# Остановка
sudo systemctl stop discord-social-bot.service
```

Для **ручного обновления** заранее запиши текущий commit SHA, останови только Discord Social Bot, обнови код и зависимости и проверь его перед повторным запуском:

```bash
sudo git -C /opt/discord-social-bot/app rev-parse HEAD
sudo systemctl stop discord-social-bot.service
sudo -u socialbot git -C /opt/discord-social-bot/app pull --ff-only
sudo -u socialbot /opt/discord-social-bot/venv/bin/python \
  -m pip install -r /opt/discord-social-bot/app/requirements.txt
sudo -u socialbot /opt/discord-social-bot/venv/bin/python \
  /opt/discord-social-bot/app/check_runtime.py
sudo -u socialbot sh -c '
  cd /opt/discord-social-bot/app &&
  ../venv/bin/python -m unittest discover -s tests -v
'
sudo systemctl start discord-social-bot.service
```

Если `git pull`, установка или тесты завершились ошибкой, **не запускай изменённую версию вслепую**. Сначала разберись в причине; при необходимости верни прежний коммит и зависимости. Файл `.env` Git не должен отслеживать.

## 8. Безопасность VPS

- Старайся использовать вход по SSH-ключам и не открывать лишние порты. Для обычной работы Discord-бота **не нужен входящий HTTP-порт**.
- Храни токен в `.env` с правами `600` и не публикуй его.
- Устанавливай обновления безопасности ОС и делай резервную копию настроек вне VPS.
- Ограничивай права Discord по каналам; `Administrator` не требуется.
- Не давай боту доступ к приватным файлам других служб.

См. [диагностику](TROUBLESHOOTING_RU.md) при проблемах с FFmpeg, авторизацией, правами и systemd.
