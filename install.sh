#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="Speed Audit RU"
APP_VERSION="1.3.0"
INSTALL_DIR="/opt/speed-audit-ru"
AUDIT_TARGET="${INSTALL_DIR}/speed-audit-ru"
BIN_TARGET="/usr/local/bin/speed-audit-ru"
CONFIG_DIR="/etc/speed-audit-ru"
CONFIG_FILE="${CONFIG_DIR}/speed-audit-ru.conf"
RAW_BASE_URL_DEFAULT="https://raw.githubusercontent.com/MALYSHVIP/speed-audit-RU/main"
LIBRESPEED_VERSION="1.0.13"
LIBRESPEED_SERVERS_URL_DEFAULT="https://librespeed.org/backend-servers/servers.php"
LIBRESPEED_SERVER_IDS_DEFAULT="102,79,85,74,87,69,28,50,51,94"
HTTP_PROBE_URLS_DEFAULT="https://mirror.yandex.ru/ubuntu/ls-lR.gz https://proof.ovh.net/files/100Mb.dat https://cachefly.cachefly.net/100mb.test https://download.docker.com/linux/ubuntu/dists/jammy/Release"

FORCE_INSTALL=0
CHECK_ONLY=0
LIBRESPEED_SERVER_IDS="${SPEED_AUDIT_LIBRESPEED_SERVER_IDS:-${LIBRESPEED_SERVER_IDS_DEFAULT}}"
LIBRESPEED_IP_VERSION="${SPEED_AUDIT_LIBRESPEED_IP_VERSION:-4}"
RAW_BASE_URL="${SPEED_AUDIT_RAW_BASE_URL:-${RAW_BASE_URL_DEFAULT}}"
LIBRESPEED_SERVERS_URL="${SPEED_AUDIT_LIBRESPEED_SERVERS_URL:-${LIBRESPEED_SERVERS_URL_DEFAULT}}"
HTTP_PROBE_URLS="${SPEED_AUDIT_HTTP_PROBE_URLS:-${HTTP_PROBE_URLS_DEFAULT}}"

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

log() {
  printf '[%s] %s\n' "${APP_NAME}" "$1"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Запустите установщик от root: sudo bash install.sh" >&2
    exit 1
  fi
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
      --force-install)
        FORCE_INSTALL=1
        ;;
      --check-only)
        CHECK_ONLY=1
        ;;
      --ru-node)
        ;;
      --librespeed-server-ids=*)
        LIBRESPEED_SERVER_IDS="${1#*=}"
        ;;
      --librespeed-ip-version=*)
        LIBRESPEED_IP_VERSION="${1#*=}"
        ;;
      --help|-h)
        cat <<'EOF_HELP'
Использование:
  curl -fsSL https://raw.githubusercontent.com/MALYSHVIP/speed-audit-RU/main/install.sh | sudo bash

Опции:
  --check-only                     Не устанавливать пакеты, а только запустить проверку.
  --force-install                  Перекачать локальный скрипт проверки и заново сверить установку.
  --librespeed-server-ids=ID1,ID2  Задать список LibreSpeed server ID через запятую.
  --librespeed-ip-version=4|6      Версия IP для LibreSpeed.
  --ru-node                        Совместимость со старой командой; режим RU включен по умолчанию.
  --help                           Показать эту справку.

Переменные окружения:
  SPEED_AUDIT_RAW_BASE_URL            Базовый raw URL для install.sh и bin/.
  SPEED_AUDIT_LOCAL_SCRIPT_SOURCE     Локальный путь до bin/speed-audit-ru для отладки.
  SPEED_AUDIT_LIBRESPEED_SERVER_IDS   Список LibreSpeed server ID через запятую.
  SPEED_AUDIT_LIBRESPEED_SERVERS_URL  URL списка LibreSpeed серверов.
  SPEED_AUDIT_LIBRESPEED_IP_VERSION   Версия IP для LibreSpeed: 4 или 6.
  SPEED_AUDIT_HTTP_PROBE_URLS         Пробные HTTP URL для резервного режима.
EOF_HELP
        exit 0
        ;;
      *)
        echo "Неизвестный аргумент: $1" >&2
        exit 1
        ;;
    esac
    shift
  done
}

resolve_local_script_source() {
  local local_override="${SPEED_AUDIT_LOCAL_SCRIPT_SOURCE:-}"
  local script_dir candidate

  if [[ -n "${local_override}" && -f "${local_override}" ]]; then
    printf '%s\n' "${local_override}"
    return 0
  fi

  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    candidate="${script_dir}/bin/speed-audit-ru"
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  fi

  return 1
}

download_audit_script() {
  local target="$1"
  local local_source raw_url

  mkdir -p "$(dirname "${target}")"

  if local_source="$(resolve_local_script_source)"; then
    log "Беру локальный скрипт проверки из ${local_source}."
    cp "${local_source}" "${target}"
  else
    raw_url="${RAW_BASE_URL}/bin/speed-audit-ru"
    log "Загружаю актуальный скрипт проверки из ${raw_url}."
    curl -fsSL "${raw_url}" -o "${target}"
  fi

  chmod +x "${target}"
}

fix_packagekit_if_needed() {
  log "Пробую убрать проблему с PackageKit, если она мешает apt."

  if command_exists systemctl; then
    systemctl unmask packagekit.service 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
    systemctl start packagekit.service 2>/dev/null || true
  fi

  if dpkg -s packagekit >/dev/null 2>&1; then
    apt-get -y purge packagekit packagekit-tools >/dev/null 2>&1 || true
  fi
}

safe_apt_update() {
  if apt-get update -y; then
    return 0
  fi

  fix_packagekit_if_needed
  apt-get update -y
}

install_packages_if_needed() {
  local packages=("$@")

  if ((${#packages[@]} == 0)); then
    return 0
  fi

  safe_apt_update
  log "Устанавливаю системные зависимости: ${packages[*]}"
  apt-get install -y "${packages[@]}"
}

ensure_runtime_packages() {
  local packages=()

  export DEBIAN_FRONTEND=noninteractive

  if ! command_exists curl; then
    packages+=("curl")
  fi
  if ! command_exists jq; then
    packages+=("jq")
  fi
  if ! command_exists tar; then
    packages+=("tar")
  fi
  if ! dpkg -s ca-certificates >/dev/null 2>&1; then
    packages+=("ca-certificates")
  fi

  install_packages_if_needed "${packages[@]}"
}

librespeed_asset_name() {
  case "$(dpkg --print-architecture)" in
    amd64)
      printf 'linux_amd64\n'
      ;;
    arm64)
      printf 'linux_arm64\n'
      ;;
    armhf)
      printf 'linux_armv7\n'
      ;;
    i386)
      printf 'linux_386\n'
      ;;
    *)
      return 1
      ;;
  esac
}

install_librespeed_cli_if_needed() {
  local asset_suffix download_url tmpdir

  if command_exists librespeed-cli; then
    log "LibreSpeed CLI уже найден. Повторно не устанавливаю."
    return 0
  fi

  asset_suffix="$(librespeed_asset_name)" || {
    echo "Неподдерживаемая архитектура для LibreSpeed CLI: $(dpkg --print-architecture)" >&2
    exit 1
  }

  download_url="https://github.com/librespeed/speedtest-cli/releases/download/v${LIBRESPEED_VERSION}/librespeed-cli_${LIBRESPEED_VERSION}_${asset_suffix}.tar.gz"
  tmpdir="$(mktemp -d)"

  log "Ставлю LibreSpeed CLI ${LIBRESPEED_VERSION}."
  curl -fsSL "${download_url}" -o "${tmpdir}/librespeed-cli.tar.gz"
  tar -xzf "${tmpdir}/librespeed-cli.tar.gz" -C "${tmpdir}"
  install -m 0755 "${tmpdir}/librespeed-cli" /usr/local/bin/librespeed-cli
  rm -rf "${tmpdir}"
}

write_runtime_config() {
  mkdir -p "${CONFIG_DIR}"

  cat > "${CONFIG_FILE}" <<EOF
SPEED_AUDIT_NET_BACKEND="librespeed"
SPEED_AUDIT_LIBRESPEED_SERVER_IDS="${LIBRESPEED_SERVER_IDS}"
SPEED_AUDIT_LIBRESPEED_IP_VERSION="${LIBRESPEED_IP_VERSION}"
SPEED_AUDIT_LIBRESPEED_SERVERS_URL="${LIBRESPEED_SERVERS_URL}"
SPEED_AUDIT_HTTP_PROBE_URLS="${HTTP_PROBE_URLS}"
EOF
}

install_local_wrapper() {
  mkdir -p "${INSTALL_DIR}"
  download_audit_script "${AUDIT_TARGET}"
  ln -sfn "${AUDIT_TARGET}" "${BIN_TARGET}"
}

runtime_ready() {
  command_exists jq && command_exists curl && command_exists librespeed-cli && [[ -x "${BIN_TARGET}" ]]
}

run_check() {
  if [[ ! -x "${BIN_TARGET}" ]]; then
    echo "Скрипт проверки не найден: ${BIN_TARGET}" >&2
    exit 1
  fi

  exec "${BIN_TARGET}"
}

main() {
  parse_args "$@"

  if [[ "${CHECK_ONLY}" -eq 1 ]]; then
    log "Запущен режим только проверки."
    run_check
  fi

  require_root
  write_runtime_config

  if [[ "${FORCE_INSTALL}" -eq 0 ]] && runtime_ready; then
    log "Все нужные компоненты уже установлены. Сразу перехожу к проверке."
    run_check
  fi

  log "Обновляю локальный скрипт проверки."
  install_local_wrapper

  log "Проверяю базовые системные зависимости."
  ensure_runtime_packages

  log "Проверяю LibreSpeed CLI и резервные сетевые режимы для RU-нод."
  install_librespeed_cli_if_needed

  log "Запускаю итоговую проверку сервера."
  run_check
}

main "$@"
