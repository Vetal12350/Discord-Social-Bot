# CLAUDE.md

## Плагины

- Перед использованием API библиотеки Discord проверяй актуальную документацию через Context7, старые примеры часто ломаются.
- Тексты сообщений бота пиши и чисти через humanizer, они должны звучать по-человечески.
- Для падений и ошибок используй debugging-and-error-recovery, для логов: observability-and-instrumentation.
- Токен бота и ключи никогда не коммить и не выводить в логи.
- Вопросы об устройстве проекта: сначала `graphify-out/`, если папка есть. Её не коммитить (она в `.gitignore`).

## Безопасность
- Токен, ключи, пароли, содержимое `.env` не коммитить, не писать в логи и не показывать в ответах.
- В логи идут только фиксированные категории и `source_key`, без сырого текста ошибок провайдеров и URL CDN (подписанные ссылки). Пример: `SAFE_MEDIA_WORKER_REASONS` в `media.py`.
- Merge в `main` и публикация — только после явного «да» владельца.

## Проверки
- Тесты: `python -m unittest discover -s tests` (нужны зависимости из `requirements.txt`, лучше в venv).
- Как в CI: `python -m compileall -q .` и `python -c 'import animation, media, bot, config, social_posts, webhook_repost'`.

## Ключевые файлы
- `bot.py` (VideoBot, обработка сообщений), `config.py` (`.env`), `link_utils.py` (разбор ссылок), `social_posts.py` (метаданные и подписи), `webhook_repost.py` (отправка).
- `media.py` (запуск воркера, лимиты, диагностика), `media_worker.py` (одноразовый yt-dlp), `tiktok_requests.py`, `tiktok_photos.py`, `safe_http.py`, `browser_http.py` (сеть с allowlist).
- `tools/weekly_stats.py` — офлайн-разбор журнала; `docs/` — инструкции и аудиты TikTok.

## Текущее состояние (на 23.11.2026)

### Сделано
- #19 зеркала слайдов TikTok при 403/404 — выпущено на VPS, пост на 9 слайдов проверен в Discord.
- #20 зеркала CDN для TikTok-видео при 403/404 (тот же формат, до 3 копий), #21 Dependabot (yt-dlp ежедневно, остальное minor/patch), #18 `tools/weekly_stats.py [--details]` — слиты в `main` (`e3b2da8`). Обновление VPS до `e3b2da8` владелец не подтвердил.
- #24 (этот файл и `.claude/settings.json`) — слит в `main` (`9625c69`).
- #25 (этот раздел, `graphify-out/` в `.gitignore`), #22 `actions/checkout` 4→7 и #23 `actions/setup-python` 5→7 — слиты в `main` (`a1c271e`), CI зелёный на 3.13/3.14.

### Решения и почему
- Очередь сообщений не нужна: отказов `repost skipped` по inflight не было.
- Работаем по факту сбоя, без еженедельных отчётов; `weekly_stats.py --details` — для разовых разборов.
- Галерея отправляется только целиком: неполную не публикуем.
- Не делать: мобильный API TikTok (нужны `app_info`/`device_id` приложения), резидентные прокси и cookies аккаунта — ломают модель безопасности; повтор с профилем Safari — нет данных, что поможет.
- PR #9 (Shorts) устарел и не мёржится: yt-dlp без JS-движка, deno не влезает в лимиты worker.
- Merge — только по явному разрешению владельца на конкретный PR.

### Осталось
- Больше 10 картинок несколькими сообщениями (TikTok до 35 слайдов, X-пост на 20 картинок `gabei_swqazx/status/2121543668607942804`). Ждёт решения владельца: удалять ли уже отправленные части при сбое (рекомендовано да) и предел 30.
- YouTube Shorts: прогнать на VPS `yt-dlp -s -F` с реальным ID; выбрать вариант A (перевод через oEmbed, плеер Discord) или B (скачивание).
- Проверить на VPS видео `vt.tiktok.com/ZSpb3bLxF` после выпуска #20: в журнале ищи `media_worker_mirror`.
- Автовыпуск (#29): владелец должен один раз запустить `tools/vps_deploy_bootstrap.sh` и завести environment `production` с секретами.
- Ветка `main` не защищена; включены ли secret scanning и push protection — неизвестно.

### Риски
- 403 `html_challenge` на CDN TikTok может остаться, если подпись привязана к сессии или IP: польза зеркал не доказана.
- `MediaTooLarge` логируется только как `Webhook repost incomplete`, без `repost_failure`.
- Лимит размера считается на всё сообщение: `min(19 МиБ, лимит сервера)`, без бустов — 10 МБ.
- Фото из цитируемого твита (quote) не пересылаются.
- Старая копия v1 в OneDrive у владельца (`дд бот`), возможно с `.env` в облаке.
- На VPS 44 обновления ОС и требуется перезагрузка.

### Выпуск на VPS
- После настройки (`docs/VPS_AUTO_UPDATE_RU.md`) выпускает агент: после «да» владельца на merge запусти workflow `deploy.yml` (`sha` = голова `main`, `action=deploy`) и дождись `DEPLOYED`. Откат — `action=rollback` с текущим SHA.
- Пока environment `production` не настроен, выпуск — вручную по шагам ниже.

### Как работать с VPS (команды даёт агент, выполняет владелец)
- Вход: `ssh socialbot` (алиас в `~/.ssh/config` владельца).
- git на VPS — только `sudo -u socialbot git -C /opt/discord-social-bot/app …`; журнал — через `sudo journalctl`.
- Часы VPS в UTC, Киев = UTC+2.
- Выпуск: `pull --ff-only` → `unittest discover -s tests` → `systemctl restart` → `ActiveState`/`NRestarts`/SHA; при падении тестов — `reset --hard` на прежний SHA без перезапуска.
