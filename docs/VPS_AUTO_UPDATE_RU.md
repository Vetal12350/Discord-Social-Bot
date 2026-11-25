# Обновление VPS из GitHub Actions

Код: [deploy_agent.py](../tools/deploy_agent.py), [vps_deploy_bootstrap.sh](../tools/vps_deploy_bootstrap.sh), [deploy.yml](../.github/workflows/deploy.yml). Тесты: `tests/test_deploy_agent.py` (без SSH и без сервера).

## Как это работает

1. Владелец вливает PR в `main`. Это и есть решение о выпуске.
2. Агент Claude (или владелец с телефона: GitHub → Actions → **Deploy to VPS** → Run workflow) запускает workflow с точным SHA текущего `main`.
3. `verify` проверяет, что SHA — голова `main` и что CI по нему зелёный.
4. `run` один раз отправляет по SSH `deploy <sha>`. SSH-ключ лежит только в environment `production` и на сервере даёт ровно три команды: `deploy`, `rollback`, `status`.
5. Агент на сервере, отдельным процессом:
   - если изменился `requirements.txt`, ставит новые зависимости;
   - гоняет тесты в отдельной копии кода (`/opt/discord-social-bot/deploy-test`), живую папку не трогает;
   - при зелёных тестах делает `git reset --hard <sha>` и перезапускает службу;
   - ждёт в журнале `Connected to Discord` и ещё 20 секунд без перезапусков systemd.
6. Бот не поднялся → агент сам возвращает прежний коммит и зависимости. Откат тоже не удался → `NEEDS_OWNER`: новые операции блокируются, пока владелец не разберётся.

git, pip и тесты агент запускает от `socialbot`, не от root. `.env` он не читает и в ответах пишет только фиксированные слова.

| Ответ | Значит |
| --- | --- |
| `DEPLOYED` | выпущено, бот подключился |
| `FAILED` | тесты не прошли, бот не трогали |
| `ROLLED_BACK` | новая версия не поднялась, вернули прежнюю |
| `NEEDS_OWNER` | откат не удался, нужен владелец |
| `BUSY` | идёт другая операция или висит `NEEDS_OWNER` |
| `REJECTED …` | запрос не принят, ничего не менялось |

Ручной откат: тот же workflow с `action=rollback` и SHA, который сейчас установлен.

## Разовая настройка (владелец)

1. На сервере (`<COMMIT>` назовёт агент):
   ```bash
   curl -fsSLo /tmp/dsb-bootstrap.sh https://raw.githubusercontent.com/Vetal12350/Discord-Social-Bot/<COMMIT>/tools/vps_deploy_bootstrap.sh
   sudo bash /tmp/dsb-bootstrap.sh --ref <COMMIT>
   ```
   Скрипт бота не перезапускает и `.env` не читает. В конце он печатает секреты.
2. GitHub → Settings → Environments → **New environment** `production`. В Deployment branches оставь только `main`. В Environment secrets добавь `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_SSH_KEY` (и `DEPLOY_PORT`, если скрипт его напечатал). **В чат их не присылай.**
3. Удали ключ с сервера командой, которую напечатал скрипт.
4. Первый выпуск делай, когда сам рядом.

## Если `NEEDS_OWNER`

Посмотри `systemctl status discord-social-bot.service` и `sudo -u socialbot git -C /opt/discord-social-bot/app log -1`. Верни рабочий коммит через `sudo -u socialbot git -C /opt/discord-social-bot/app reset --hard <SHA>` и перезапусти службу. Потом удали запись задачи: `sudo rm /var/lib/discord-social-bot-deploy/<sha>.json`. Только после этого агент примет новую операцию.

## Остаточные риски

- Кто получит ключ **и** право записи в `main`, тот сможет выкатить свой код. Право записи держи только у себя.
- Агент на сервере обновляется только повторным запуском скрипта настройки. Сам себя из репозитория он не обновляет.
- Новые зависимости ставятся в общий venv до тестов. Если тесты упали, агент возвращает прежние версии, но пакеты, которых раньше не было, остаются в venv.
