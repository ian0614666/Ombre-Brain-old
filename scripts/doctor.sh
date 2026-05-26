#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_ops_common.sh"

cd "$(ombre_repo_root)"

COMPOSE_FILE="$(ombre_compose_file)"
OMBRE_SERVICE="${OMBRE_SERVICE:-ombre-brain}"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-ombre-gateway}"
HEALTH_URL="${HEALTH_URL:-$(ombre_default_health_url "${COMPOSE_FILE}")}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-5}"
LOG_TAIL="${LOG_TAIL:-160}"

issues=0
warnings=0

section() {
  printf '\n== %s ==\n' "$1"
}

ok() {
  printf 'OK   %s\n' "$1"
}

warn() {
  warnings=$((warnings + 1))
  printf 'WARN %s\n' "$1"
}

fail() {
  issues=$((issues + 1))
  printf 'FAIL %s\n' "$1"
}

info() {
  printf 'INFO %s\n' "$1"
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

service_in_compose() {
  local service="$1"
  grep -Eq "^[[:space:]]{0,2}${service}:" "${COMPOSE_FILE}"
}

env_has_value() {
  local key="$1"
  if [[ -n "${!key:-}" ]]; then
    return 0
  fi
  if [[ ! -f ".env" ]]; then
    return 1
  fi
  awk -v key="${key}" '
    BEGIN { found = 1 }
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      split(line, pair, "=")
      name = pair[1]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name != key) next
      value = substr(line, index(line, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^["'\'']|["'\'']$/, "", value)
      if (value != "" && value !~ /^(your-key|changeme|replace-me|xxx)$/) {
        found = 0
      }
    }
    END { exit found }
  ' ".env"
}

print_env_status() {
  local key="$1"
  local note="${2:-}"
  if env_has_value "${key}"; then
    ok "${key} 已配置${note}"
  else
    warn "${key} 未配置${note}"
  fi
}

extract_config_key_envs() {
  [[ -f "config.yaml" ]] || return 0
  awk '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*api_key_env:/ {
      value = $0
      sub(/.*api_key_env:[[:space:]]*/, "", value)
      gsub(/["'\''[:space:]]/, "", value)
      if (value != "") print value
      next
    }
    /^[[:space:]]*api_key_envs:/ { in_list = 1; next }
    in_list && /^[[:space:]]*-[[:space:]]*/ {
      value = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", value)
      gsub(/["'\''[:space:]]/, "", value)
      if (value != "") print value
      next
    }
    in_list && /^[^[:space:]-]/ { in_list = 0 }
  ' "config.yaml" | sort -u
}

compose_ps() {
  ombre_compose -f "${COMPOSE_FILE}" ps "$@" 2>/dev/null
}

container_env_status() {
  local service="$1"
  shift
  if ! service_in_compose "${service}"; then
    return 0
  fi
  if ! compose_ps "${service}" >/dev/null 2>&1; then
    return 0
  fi
  ombre_compose -f "${COMPOSE_FILE}" exec -T "${service}" sh -lc '
    for key in "$@"; do
      if [ -n "$(printenv "$key")" ]; then
        echo "OK   container:$key 已读到"
      else
        echo "WARN container:$key 未读到"
      fi
    done
  ' sh "$@" 2>/dev/null || true
}

try_health() {
  local url="$1"
  if curl -fsS --max-time "${HEALTH_TIMEOUT}" "${url}" >/dev/null 2>&1; then
    ok "健康检查通过：${url}"
    return 0
  fi
  return 1
}

print_ports_hint() {
  info "compose 里的 ports："
  awk '
    function indent(line) {
      match(line, /[^ ]/)
      return RSTART ? RSTART - 1 : 0
    }
    /^[[:space:]]*ports:/ {
      in_ports = 1
      ports_indent = indent($0)
      print "  " $0
      next
    }
    in_ports {
      current_indent = indent($0)
      if ($0 !~ /^[[:space:]]*$/ && current_indent <= ports_indent) {
        in_ports = 0
      }
    }
    in_ports && /^[[:space:]]*-[[:space:]]*/ { print "  " $0; next }
  ' "${COMPOSE_FILE}" || true
}

print_log_findings() {
  local service="$1"
  if ! service_in_compose "${service}"; then
    return 0
  fi
  if ! compose_ps "${service}" >/dev/null 2>&1; then
    return 0
  fi

  local matches
  matches="$(
    ombre_compose -f "${COMPOSE_FILE}" logs --tail="${LOG_TAIL}" "${service}" 2>&1 \
      | grep -Eai 'error|exception|traceback|401|403|429|500|502|503|504|connection refused|address already in use|api key|unauthorized|permission denied|timeout' \
      | tail -n 24 \
      | sed -E \
          -e 's/(Bearer )[A-Za-z0-9._~+\/=-]+/\1***/g' \
          -e 's/(Authorization: )[A-Za-z0-9._~+\/=-]+/\1***/g' \
          -e 's/((api_key|apikey|token|secret)[=:][[:space:]]*)[^[:space:]]+/\1***/g'
  )"

  if [[ -n "${matches}" ]]; then
    warn "${service} 最近 ${LOG_TAIL} 行日志里有疑似错误："
    printf '%s\n' "${matches}" | sed 's/^/  /'
  else
    ok "${service} 最近 ${LOG_TAIL} 行日志没有明显错误关键词"
  fi
}

section "基础环境"
info "Repo: $(pwd)"
info "Compose: ${COMPOSE_FILE}"

if has_command docker; then
  ok "docker 命令存在"
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon 可用"
  else
    fail "Docker daemon 不可用。先启动 Docker，再重试。"
  fi
else
  fail "未找到 docker 命令"
fi

if docker compose version >/dev/null 2>&1 || has_command docker-compose; then
  ok "Docker Compose 可用"
else
  fail "未找到 docker compose / docker-compose"
fi

if has_command curl; then
  ok "curl 可用"
else
  warn "未找到 curl，健康检查会跳过"
fi

section "配置和 key"
if [[ -f ".env" ]]; then
  ok ".env 存在"
else
  warn ".env 不存在。Docker 部署通常需要在这里放 API key。"
fi

if [[ -f "config.yaml" ]]; then
  ok "config.yaml 存在"
else
  warn "config.yaml 不存在。请先从 config.example.yaml 复制并按需修改。"
fi

print_env_status "OMBRE_API_KEY" "（脱水/导入抽取默认使用）"
print_env_status "OMBRE_EMBEDDING_API_KEY" "（embedding 独立 key，未配时可能回退脱水 key）"
print_env_status "OMBRE_DREAM_API_KEY" "（夜梦模型 key；没开夜梦可忽略）"
if service_in_compose "${GATEWAY_SERVICE}"; then
  print_env_status "OMBRE_GATEWAY_TOKEN" "（Gateway 对外鉴权 token）"
else
  info "compose 未启用 ${GATEWAY_SERVICE}，跳过 Gateway token 检查"
fi

extra_keys="$(extract_config_key_envs || true)"
if [[ -n "${extra_keys}" ]]; then
  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    print_env_status "${key}" "（config.yaml 里 gateway.upstreams 引用）"
  done <<< "${extra_keys}"
fi

section "服务状态"
if has_command docker && docker info >/dev/null 2>&1; then
  if compose_ps >/dev/null 2>&1; then
    ombre_compose -f "${COMPOSE_FILE}" ps
  else
    fail "无法读取 compose 状态。确认 ${COMPOSE_FILE} 是否属于当前部署。"
  fi

  if service_in_compose "${OMBRE_SERVICE}"; then
    if compose_ps "${OMBRE_SERVICE}" | grep -qi "running\|up"; then
      ok "${OMBRE_SERVICE} 正在运行"
    else
      fail "${OMBRE_SERVICE} 没有运行。可执行：COMPOSE_FILE=${COMPOSE_FILE} bash scripts/update_deploy.sh"
    fi
  else
    fail "compose 里没有服务 ${OMBRE_SERVICE}。如服务名不同，请设置 OMBRE_SERVICE=你的服务名。"
  fi

  if service_in_compose "${GATEWAY_SERVICE}"; then
    if compose_ps "${GATEWAY_SERVICE}" | grep -qi "running\|up"; then
      ok "${GATEWAY_SERVICE} 正在运行"
    else
      warn "${GATEWAY_SERVICE} 没有运行；如果不用 Gateway 可以忽略。"
    fi
  fi
else
  warn "跳过 Docker 服务状态检查"
fi

section "容器内 key"
if has_command docker && docker info >/dev/null 2>&1; then
  if service_in_compose "${GATEWAY_SERVICE}"; then
    container_env_status "${OMBRE_SERVICE}" \
      OMBRE_API_KEY OMBRE_EMBEDDING_API_KEY OMBRE_DREAM_API_KEY OMBRE_GATEWAY_TOKEN
  else
    container_env_status "${OMBRE_SERVICE}" \
      OMBRE_API_KEY OMBRE_EMBEDDING_API_KEY OMBRE_DREAM_API_KEY
  fi
else
  warn "Docker 不可用，跳过容器内环境变量检查"
fi

section "端口和健康检查"
if has_command curl; then
  if ! try_health "${HEALTH_URL}"; then
    warn "默认健康地址不通：${HEALTH_URL}"
    found_health=0
    for candidate in \
      "http://127.0.0.1:18001/health" \
      "http://127.0.0.1:8000/health" \
      "http://127.0.0.1:18002/health"; do
      [[ "${candidate}" == "${HEALTH_URL}" ]] && continue
      if try_health "${candidate}"; then
        found_health=1
      fi
    done
    if [[ "${found_health}" == "0" ]]; then
      fail "常见健康端口都不通。优先看服务是否启动、ports 是否映射正确。"
      print_ports_hint
    else
      warn "服务可能在别的端口上。可以用 HEALTH_URL=正确地址 bash scripts/doctor.sh 复查。"
    fi
  fi
else
  warn "curl 不可用，跳过健康检查"
fi

section "最近错误"
if has_command docker && docker info >/dev/null 2>&1; then
  print_log_findings "${OMBRE_SERVICE}"
  print_log_findings "${GATEWAY_SERVICE}"
else
  warn "Docker 不可用，跳过日志检查"
fi

section "下一步"
if (( issues > 0 )); then
  info "发现 ${issues} 个需要先处理的问题，${warnings} 个提醒。"
  info "常用修复命令："
  printf '  COMPOSE_FILE=%s bash scripts/update_deploy.sh\n' "${COMPOSE_FILE}"
  printf '  docker compose -f %s logs --tail=200 %s\n' "${COMPOSE_FILE}" "${OMBRE_SERVICE}"
  printf '  HEALTH_URL=http://127.0.0.1:18001/health bash scripts/doctor.sh\n'
  exit 1
fi

if (( warnings > 0 )); then
  info "没有硬错误，但有 ${warnings} 个提醒。"
else
  ok "未发现明显问题"
fi
