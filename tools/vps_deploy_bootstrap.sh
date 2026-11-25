#!/usr/bin/env bash
# Discord Social Bot: one-time VPS setup for deploys from GitHub Actions.
# See docs/VPS_AUTO_UPDATE_RU.md. Run ONCE by the owner, from a downloaded copy:
#   curl -fsSLo /tmp/dsb-bootstrap.sh \
#     https://raw.githubusercontent.com/Vetal12350/Discord-Social-Bot/<COMMIT>/tools/vps_deploy_bootstrap.sh
#   sudo bash /tmp/dsb-bootstrap.sh --ref <COMMIT>
#
# Idempotent. Never restarts or stops the bot and never reads .env. It:
#  1. checks the existing layout (service, socialbot, /opt/discord-social-bot);
#  2. creates user discord-deploy (login only through the forced command);
#  3. installs tools/deploy_agent.py from the SAME commit as a root-owned file;
#  4. adds a sudoers rule for exactly that file (checked with visudo);
#  5. creates a dedicated SSH key and prints the GitHub secrets.
set -euo pipefail
umask 077

REPO_RAW="https://raw.githubusercontent.com/Vetal12350/Discord-Social-Bot"
SERVICE="discord-social-bot.service"
DEPLOY_USER="discord-deploy"
AGENT="/usr/local/sbin/discord-social-bot-deploy"
STATE="/var/lib/discord-social-bot-deploy"
SUDOERS="/etc/sudoers.d/discord-social-bot-deploy"
HOME_DIR="/home/$DEPLOY_USER"
KEY="$STATE/github-actions-key"
REF=""
HOST=""

die() { echo "ОШИБКА: $*" >&2; echo "Бот не перезапускался. Исправьте и запустите снова." >&2; exit 1; }
say() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done
[[ "$REF" =~ ^[0-9a-f]{40}$ ]] || die "нужен --ref <40-символьный commit>"
[ "$(id -u)" = 0 ] || die "запускайте через sudo"

say "Проверка сервера"
[ "$(systemctl show -p LoadState --value "$SERVICE")" = "loaded" ] || die "$SERVICE не найден"
[ "$(systemctl show -p User --value "$SERVICE")" = "socialbot" ] || die "служба должна работать от socialbot"
[ "$(systemctl show -p WorkingDirectory --value "$SERVICE")" = "/opt/discord-social-bot/app" ] \
  || die "ожидался WorkingDirectory=/opt/discord-social-bot/app"
[ -x /opt/discord-social-bot/venv/bin/python ] || die "нет /opt/discord-social-bot/venv"
runuser -u socialbot -- git -C /opt/discord-social-bot/app rev-parse HEAD >/dev/null \
  || die "/opt/discord-social-bot/app не git-репозиторий socialbot"
command -v systemd-run >/dev/null && command -v visudo >/dev/null && command -v ssh-keygen >/dev/null \
  || die "нужны systemd-run, visudo и ssh-keygen"

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  say "Создаю пользователя $DEPLOY_USER"
  # No password is set; a shell is needed only to run the forced command.
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$HOME_DIR/.ssh"

say "Устанавливаю агент из коммита ${REF:0:12}"
tmp_agent="$(mktemp)"
curl -fsSL "$REPO_RAW/$REF/tools/deploy_agent.py" -o "$tmp_agent"
sed -i '1s|.*|#!/usr/bin/python3 -I|' "$tmp_agent"
/usr/bin/python3 -I -m py_compile "$tmp_agent" || die "агент не компилируется"
install -D -o root -g root -m 0755 "$tmp_agent" "$AGENT"
rm -f "$tmp_agent"
install -d -o root -g root -m 0700 "$STATE"

sudo_tmp="$(mktemp)"
cat > "$sudo_tmp" <<EOF
Defaults!$AGENT env_keep += "SSH_ORIGINAL_COMMAND"
$DEPLOY_USER ALL=(root) NOPASSWD: $AGENT ""
EOF
visudo -cf "$sudo_tmp" >/dev/null || die "правило sudo не прошло visudo"
install -o root -g root -m 0440 "$sudo_tmp" "$SUDOERS"
rm -f "$sudo_tmp"

if ! grep -qs "$AGENT" "$HOME_DIR/.ssh/authorized_keys"; then
  say "Создаю отдельный SSH-ключ для GitHub Actions"
  rm -f "$KEY" "$KEY.pub"
  ssh-keygen -q -t ed25519 -N "" -C "discord-social-bot-github-actions" -f "$KEY"
  printf 'restrict,command="sudo -n %s" %s\n' "$AGENT" "$(cat "$KEY.pub")" > "$HOME_DIR/.ssh/authorized_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$HOME_DIR/.ssh/authorized_keys"
  chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
fi
if sshd -T 2>/dev/null | grep -qiE '^allowusers ' && ! sshd -T 2>/dev/null | grep -iE '^allowusers ' | grep -qw "$DEPLOY_USER"; then
  echo "ВНИМАНИЕ: в sshd задан AllowUsers без $DEPLOY_USER — добавьте его вручную." >&2
fi

[ -n "$HOST" ] || HOST="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
[ -n "$HOST" ] || die "не удалось определить IPv4; запустите с --host <адрес>"
PORT="$(sshd -T 2>/dev/null | sed -n 's/^port //p' | head -1)"
PORT="${PORT:-22}"
known="$HOST"
[ "$PORT" = 22 ] || known="[$HOST]:$PORT"

echo
echo "================ ГОТОВО. Бот продолжает работать как раньше. ================"
echo "GitHub → Settings → Environments → production → Environment secrets."
echo "Не присылайте эти значения в чат."
echo
echo "DEPLOY_HOST:  $HOST"
echo "DEPLOY_USER:  $DEPLOY_USER"
[ "$PORT" = 22 ] || echo "DEPLOY_PORT:  $PORT"
echo "DEPLOY_KNOWN_HOSTS:"
echo "$known $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
if [ -f "$KEY" ]; then
  echo "DEPLOY_SSH_KEY (всё, включая строки BEGIN/END):"
  cat "$KEY"
  echo
  echo "После копирования удалите ключ с сервера:  sudo rm -f $KEY $KEY.pub"
fi
