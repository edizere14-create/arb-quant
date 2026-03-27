#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
LOG_DIR="$ROOT_DIR/.logs"

usage() {
  cat <<'EOF'
Usage:
  bash rotate_keys.sh KEY=value [KEY=value ...]

Example:
  bash rotate_keys.sh TELEGRAM_TOKEN=123:abc TELEGRAM_CHAT_ID=456

Notes:
  - The script updates or appends keys in .env.
  - It restarts main_bot.py and the Streamlit HUD.
  - It clears history files it can reach from the current shell environment.
EOF
}

pick_python() {
  if [[ -n "${VENV_PYTHON:-}" && -x "${VENV_PYTHON}" ]]; then
    printf '%s' "${VENV_PYTHON}"
    return
  fi

  if [[ -x "$ROOT_DIR/.venv-1/Scripts/python.exe" ]]; then
    printf '%s' "$ROOT_DIR/.venv-1/Scripts/python.exe"
    return
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi

  echo "python executable not found" >&2
  exit 1
}

update_env_key() {
  local key="$1"
  local value="$2"
  local tmp_file
  tmp_file="$(mktemp)"

  if [[ -f "$ENV_FILE" ]]; then
    grep -v "^${key}=" "$ENV_FILE" > "$tmp_file" || true
  fi

  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  mv "$tmp_file" "$ENV_FILE"
}

restart_processes() {
  mkdir -p "$LOG_DIR"

  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'main_bot.py|streamlit run hud.py|streamlit run dashboard.py' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" >/dev/null 2>&1 || true
  else
    pkill -f "main_bot.py" >/dev/null 2>&1 || true
    pkill -f "streamlit.*hud.py" >/dev/null 2>&1 || true
    pkill -f "streamlit.*dashboard.py" >/dev/null 2>&1 || true
  fi

  local python_bin
  python_bin="$(pick_python)"

  nohup "$python_bin" "$ROOT_DIR/main_bot.py" --passive > "$LOG_DIR/main_bot.out" 2>&1 &
  nohup "$python_bin" -m streamlit run "$ROOT_DIR/hud.py" > "$LOG_DIR/hud.out" 2>&1 &
}

clear_history_files() {
  history -c >/dev/null 2>&1 || true

  if [[ -n "${HISTFILE:-}" ]]; then
    : > "$HISTFILE" || true
  fi

  rm -f "$HOME/.bash_history" "$HOME/.zhistory" "$HOME/.python_history" >/dev/null 2>&1 || true
  unset HISTFILE || true
}

main() {
  if [[ "$#" -lt 1 ]]; then
    usage
    exit 1
  fi

  cd "$ROOT_DIR"

  local backup_file="$ROOT_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
  if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$backup_file"
  fi

  local arg key value
  for arg in "$@"; do
    if [[ "$arg" != *=* ]]; then
      echo "invalid argument: $arg" >&2
      usage
      exit 1
    fi
    key="${arg%%=*}"
    value="${arg#*=}"
    update_env_key "$key" "$value"
  done

  restart_processes
  clear_history_files

  echo "Updated keys in $ENV_FILE"
  if [[ -f "${backup_file}" ]]; then
    echo "Backup saved to ${backup_file}"
  fi
  echo "Restarted main_bot.py and Streamlit HUD"
  echo "History clear attempted for current shell environment"
}

main "$@"