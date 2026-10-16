#!/usr/bin/env bash
# Run from an extracted release or a checkout: bash install.sh
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Docker bulunamadı. Docker Desktop veya Docker Engine kurun; bilgisayarsız kullanım için QUICKSTART.md içindeki Render seçeneğini kullanın.' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Docker Compose v2 gerekli. Docker Desktop güncellemesi veya Docker Compose eklentisi kurun.' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'Docker hizmeti çalışmıyor veya bu kullanıcının erişimi yok. Docker hizmetini başlatıp tekrar çalıştırın.' >&2
  exit 1
fi

if [[ ! -e .env ]]; then
  umask 077
  temporary_env="$(mktemp "$script_dir/.env.install.XXXXXXXX")"
  trap 'rm -f -- "${temporary_env:-}"' EXIT
  create_secret() {
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  }
  admin_password="$(create_secret)"
  setup_token="$(create_secret)"
  if [[ ${#admin_password} -ne 64 || ${#setup_token} -ne 64 ]]; then
    printf '%s\n' 'Güvenli parola oluşturulamadı; kurulum durduruldu.' >&2
    exit 1
  fi
  {
    printf '%s\n' '# Bu dosya gizlidir. Kaynak kontrolüne eklemeyin veya paylaşmayın.'
    printf 'APP_PASSWORD=%s\n' "$admin_password"
    printf 'JARVIS_SETUP_TOKEN=%s\n' "$setup_token"
    printf '%s\n' 'JARVIS_PORT=8080' 'JARVIS_BIND_HOST=127.0.0.1'
  } > "$temporary_env"
  # ln does not overwrite a configuration created by another installer.
  if ! ln "$temporary_env" .env 2>/dev/null; then
    printf '%s\n' 'Mevcut .env korunuyor.'
  fi
  rm -f -- "$temporary_env"
  unset admin_password setup_token
fi

docker compose config --quiet
docker compose up --build --detach --wait --wait-timeout 180
printf '\n%s\n' 'JARVIS çalışıyor. Bağlantı:'
docker compose port jarvis 8080
printf '%s\n' "Yönetim parolası: $script_dir/.env dosyasındaki APP_PASSWORD alanı." \
  'Tarayıcıda http://127.0.0.1:8080 açın (portu değiştirdiyseniz yeni portu kullanın).' \
  'Panelden servis API bilgilerinizi girin. Parolayı veya API anahtarlarını sohbette paylaşmayın.' \
  'Bilgisayarın kapanması görevleri durdurur. Sürekli çalışma için bulut dağıtımını kullanın.'
