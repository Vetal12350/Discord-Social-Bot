# Contributing / Участие в разработке

[English](#english) · [Русский](#русский)

## English

Thanks for your interest in Discord Social Bot! Bug reports, documentation improvements and focused pull requests are welcome.

- Search existing issues before creating a new one.
- For bugs, include the OS, Python version, bot mode (`webhook` or `reply`), expected/actual behavior and **redacted** logs. Public URLs are useful only if they contain no personal information.
- Never publish `.env`, Discord bot tokens, webhook URLs or private server/user data.
- Keep pull requests focused; explain what changed and why.
- Before submitting code changes, run `python check_runtime.py` (if FFmpeg is installed) and `python -m unittest discover -s tests -v`. The preflight also checks local codec support, so report separately if your environment lacks FFmpeg.
- Do not claim a feature is supported without a reproducible test or successful manual check.
- Changes to destructive behavior (deleting source messages), network access and subprocess execution deserve special care. Preserve the existing fail-safe behavior and do not bypass upload/CPU/memory limits.

Contributions to this repository are made under the existing [MIT License](LICENSE).

## Русский

Спасибо за интерес к Discord Social Bot! Можно сообщать об ошибках, улучшать документацию и предлагать изменения кода через Pull Request.

- Перед созданием Issue проверь, что аналогичная проблема ещё не описана.
- Для ошибки укажи ОС, версию Python, режим бота (`webhook`/`reply`), ожидаемое и фактическое поведение, **обезличенный** фрагмент журнала.
- **Никогда не публикуй `.env`, токены Discord, адреса вебхуков и приватные данные пользователей.**
- В Pull Request опиши внесённые изменения, причину и способ проверки.
- Перед отправкой изменений запусти `python check_runtime.py` (если доступен FFmpeg) и `python -m unittest discover -s tests -v`. Отсутствие локального FFmpeg следует указывать отдельно.
- Не добавляй неподтверждённые функции в README.
- Особое внимание уделяй логике удаления сообщений, скачиванию файлов, сетевым запросам и запуску дочерних процессов: ошибки не должны приводить к удалению оригинала или обходу ограничений ресурсов.

Все изменения распространяются в рамках действующей [лицензии MIT](LICENSE).
