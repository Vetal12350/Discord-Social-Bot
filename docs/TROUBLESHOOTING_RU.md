# Частые вопросы и устранение ошибок

[← Русский README](../README.ru.md) · [Создание и приглашение](DISCORD_SETUP_RU.md) · [VPS](VPS_RU.md)

## Бот в Discord офлайн

1. Убедись, что процесс запущен. На VPS: `systemctl is-active discord-social-bot.service`; на Windows проверь окно Python.
2. Посмотри журнал: `sudo journalctl -u discord-social-bot.service -n 80 --no-pager`.
3. Проверь, что `DISCORD_TOKEN` задан в **приватном** `.env`, который находится рядом с `bot.py`. Токен должен принадлежать именно этому Discord-приложению.
4. При `LoginFailure`/ошибке токена получи новый токен через Discord Developer Portal, **обнови `.env` и перезапусти службу**.
5. Не запускай одновременно два процесса с одним токеном (например, Altare + VPS).

## Бот онлайн, но ссылки игнорирует

- В Discord Developer Portal включи **Message Content Intent**; код также запрашивает его.
- Проверь **View Channels**, **Send Messages**, **Attach Files**, а для встраивания — **Embed Links**. Для веток также нужны **Send Messages in Threads**.
- Если заполнен `ALLOWED_CHANNEL_IDS`, бот работает только в перечисленных каналах. Пустое значение означает отсутствие такого ограничения.
- Ссылка должна быть на поддерживаемую публичную публикацию X/Twitter или TikTok. Произвольные сайты бот не обслуживает.
- Срабатывает защита от высокой нагрузки и частых запросов. Не отправляй много ссылок подряд; повтори позже.

## TikTok: браузерная загрузка на VPS

Для TikTok нужен `curl_cffi==0.17.0` (устанавливается из
`requirements.txt`). После обновления зависимостей перезапусти
**только** `discord-social-bot.service`. Для X продолжает действовать
исходный загрузчик на `urllib`.

Изолированный TikTok-загрузчик запрашивает только разрешённые HTTPS
ресурсы. Каждое перенаправление проверяется; DNS разрешается заново
перед запросом, запрещаются локальные/частные IP; libcurl получает
закреплённый проверенный IP через `CURLOPT_RESOLVE`.
Это необходимо, потому что системный аудит Python не видит
нативные соединения `curl_cffi`.

**Проверка после обновления:**

```bash
sudo -u socialbot /opt/discord-social-bot/venv/bin/python -c \
  'import yt_dlp, curl_cffi; print("TikTok browser TLS ready", curl_cffi.__version__)'
sudo journalctl -u discord-social-bot.service --since "5 minutes ago" --no-pager -o cat \
  | grep -Ei 'caption_metadata|direct_mp4|media_worker_failure|webhook_confirmation|repost_failure|source_action'
```

При неудачной загрузке исходное сообщение остаётся нетронутым.
Если сообщение содержит только обычное превью TikTok, проверь
категорию `media_worker_failure` в журнале.
Не устанавливай cookies или общедоступные прокси в бота.

## TikTok не распознаётся или остаётся только стандартное превью

Короткие адреса `vt.tiktok.com`, `vm.tiktok.com` и прямые ссылки на
`www.tiktok.com/@user/video/123...` поддерживаются. Если короткий URL
не раскрывается (403/редирект), бот пробует извлечь сведения через
`yt-dlp`, не обращаясь к сторонним небезопасным сайтам.

Если в Discord виден только стандартный TikTok-плеер, но нет
пересылки от Social Bot, это **не обязательно** ошибка распознавания.
При `REPOST_FAILURE_NOTICE=false` бот намеренно не публикует ошибку в чат.
Проверь свежие безопасные диагностические сообщения:

```bash
sudo journalctl -u discord-social-bot.service --since "15 minutes ago" --no-pager \\
  | grep -Ei 'TikTok redirect|TikTok metadata|caption_metadata|repost_failure|Webhook repost incomplete|webhook_confirmation'
```

- `TikTok metadata route=shortlink_fallback` — раскрытие короткой ссылки не удалось, пробуется резервный путь.
- `TikTok metadata result=unavailable` — метаданные не получены даже после попытки восстановления.
- `caption_metadata platform=TikTok` — текст публикации распознан (число символов указано в журнале).
- `direct_mp4_failed reason_type=...` — прямой MP4 не скачался либо несовместим; бот пробует изолированный загрузчик.
- `media_worker_failure mode=download reason=...` — классифицированная ошибка загрузчика:
  `format_unavailable` (нет совместимого MP4), `http_403`/`http_429`
  (отказ сервиса/ограничение запросов), `access_challenge` (проверка доступа),
  `extractor_failed` (источник/формат не поддержан), `network_or_policy_error`
  (сеть или защита URL), `unclassified` (иная причина). Сообщение
  об ошибке поставщика и подписанные URL в журнал не выводятся.
- `repost_failure stage=prepare` — ошибка скачивания или подготовки медиа.
- `repost_failure stage=webhook` — ошибка при отправке медиа в Discord.
- `Webhook repost incomplete ... reason=...` — уточнённая причина без раскрытия приватных данных.

Не отключай защиту URL и не вставляй токены, cookies или личные
ссылки на вебхуки в отчёты. При блокировке скачивания TikTok
резервный путь не гарантирует загрузку каждого видео.

## Webhook не работает или исходное сообщение не удалилось

- Для `REPOST_MODE=webhook` нужно **Manage Webhooks**; для удаления сообщений — **Manage Messages**.
- Бот намеренно удаляет **только сообщения с одной поддерживаемой ссылкой и без другого пользовательского содержимого**.
- Если у сообщения есть комментарий, вложения, ответ на другое сообщение либо пересылка не подтверждена, оригинал сохраняется.
- Если в логе написано `delete_source result=skipped`, не пытайся насильно удалить исходное сообщение до выяснения причины.
- Для обычного ответа от имени бота можно выбрать `REPOST_MODE=reply`.

## Фото есть, но GIF или видео не воспроизводятся

Запусти:

```bash
python check_runtime.py
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -encoders | grep -E 'libwebp_anim|[[:space:]]gif[[:space:]]'
```

Эти команды рассчитаны на Linux; на Windows `grep` можно заменить `findstr`. Если используешь `venv`, запускай `check_runtime.py` его интерпретатором Python.

- `GIF preflight: NOT READY` — проверь установку **FFmpeg/FFprobe** и доступность кодировщика WebP или GIF.
- `ModuleNotFoundError` — устанавливай зависимости через `pip install -r requirements.txt` **в том же окружении Python, которым запускаешь `bot.py`**.
- Если FFmpeg и зависимости работают, источник всё равно может отдавать неподходящее медиа либо ограничивать доступ.
- Discord также может ограничивать размер загрузки; бот дополнительно ограничивает суммарный бюджет медиа.

## Сервис пишет active, но ничего не происходит

`systemctl is-active` подтверждает только состояние процесса. Изучи:

```bash
sudo journalctl -u discord-social-bot.service -n 100 --no-pager
sudo systemctl show discord-social-bot.service -p NRestarts -p MainPID
free -h
```

Ищи `Connected to Discord` и журнал обработки ссылки. Не вставляй полные приватные журналы и токены в публичные обсуждения.

## Slash-команда /video не появилась

- При приглашении выбери scope **`applications.commands`**.
- Укажи `TEST_GUILD_ID` (ID своего сервера) для ускоренной синхронизации команды на этом сервере.
- При глобальной синхронизации изменения могут отображаться не сразу.
- Команда работает только в разрешённых каналах сервера; проверь `ALLOWED_CHANNEL_IDS`.

## Предупреждение PyNaCl is not installed

Для этого бота **голосовые функции не используются**, поэтому предупреждение о PyNaCl само по себе не мешает обработке ссылок.

## Нагрузка, память и перезапуски

- Обработка медиа и FFmpeg может кратковременно увеличивать потребление памяти и CPU.
- Если служба завершается при конвертации, посмотри `journalctl` и ограничения `MemoryMax`/`CPUQuota` в настройках `systemd`. Не убирай лимиты вслепую на VPS, где работают другие проекты.
- Рабочий бот не требует публичного сайта, keep-alive HTTP-сервера или открытого входящего порта.
- Временный медиа-кеш не является хранилищем постов и не предназначен для постоянного резервирования.

## Как получить помощь безопасно

При сообщении об ошибке укажи ОС, версию Python, режим `webhook`/`reply` и **обезличенный** фрагмент журнала. Никогда не публикуй содержимое `.env`, Discord токены и приватные ссылки вебхуков.
