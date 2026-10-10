#!/usr/bin/env bash
set -u

# name|ssh_target(- = local)|user units (space-separated)
# laptop shows lanclip in swaybar (`--swaybar`), so it runs no indicator unit.
HOSTS=(
  'laptop|-|lanclip'
  'laptop|laptop|lanclip lanclip-indicator'
  'desktop|desktop|lanclip lanclip-indicator'
)

USER_UNITS=()
LOG_LINES=40
SSH_OPTS=(
  -o ConnectTimeout=5
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
)

FAILED=0

usage() {
  cat <<'USAGE'
Usage: lanclip-ctl.sh <command> [host...] [-n LINES]

Commands:
  restart   restart the lanclip units
  start     start the lanclip units
  stop      stop the lanclip units
  status    print is-active for each unit
  logs      print recent journal lines (-n LINES, default 40)

Hosts:
  laptop    this machine
  laptop      ssh laptop
  desktop     ssh desktop
  all       every host above (the default when no host is given)

This script only controls services; it does not deploy code.
USAGE
}

log() {
  printf '[%s] %s\n' "$(date +%T)" "$*" >&2
}

host_field() {
  local name=$1
  local index=$2
  local entry

  for entry in "${HOSTS[@]}"; do
    if [ "${entry%%|*}" = "$name" ]; then
      printf '%s\n' "$entry" | cut -d'|' -f"$index"
      return 0
    fi
  done

  return 1
}

known_host() {
  host_field "$1" 1 >/dev/null 2>&1
}

# Prepare the user session for this script so `systemctl --user` works over a
# non-login ssh connection.
user_env_snippet() {
  cat <<'SNIPPET'
XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
if [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
  DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
  export DBUS_SESSION_BUS_ADDRESS
fi
SNIPPET
}

# Runs a snippet on a remote host. The snippet is passed as an argument rather
# than piped to `bash -s`.
remote_run() {
  local target=$1
  local snippet=$2
  local status

  ssh "${SSH_OPTS[@]}" "$target" "$snippet"
  status=$?

  if [ "$status" -eq 255 ]; then
    log "unreachable (ssh exit ${status})"
  elif [ "$status" -ne 0 ]; then
    log "command failed with exit ${status}"
  fi

  return "$status"
}

do_lifecycle() {
  local name=$1
  local target=$2
  local action=$3
  local status

  log "${name}: ${action} ${USER_UNITS[*]}"

  if [ "$target" = '-' ]; then
    systemctl --user "$action" "${USER_UNITS[@]}"
    status=$?
    if [ "$status" -ne 0 ]; then
      log "${name}: systemctl --user ${action} ${USER_UNITS[*]} failed with exit ${status}"
      FAILED=1
    fi

    return 0
  fi

  remote_run "$target" "$(
    user_env_snippet
    printf 'systemctl --user %s' "$action"
    printf ' %s' "${USER_UNITS[@]}"
    printf '\n'
  )" || FAILED=1
}

do_status() {
  local name=$1
  local target=$2
  local unit
  local state
  local field
  local output
  local status
  local index
  local last_index
  local -a states=()

  if [ "$target" = '-' ]; then
    for unit in "${USER_UNITS[@]}"; do
      state=$(systemctl --user is-active "$unit" 2>&1)
      states+=("$state")
    done
  else
    # `is-active` exits non-zero for inactive units, so only ssh's own 255
    # counts as unreachable here.
    output=$(ssh "${SSH_OPTS[@]}" "$target" "$(
      user_env_snippet
      for unit in "${USER_UNITS[@]}"; do
        printf 'systemctl --user is-active %s\n' "$unit"
      done
    )" 2>/dev/null)
    status=$?
    if [ "$status" -eq 255 ] || [ -z "$output" ]; then
      FAILED=1
      printf '%-7s unreachable (ssh exit %s)\n' "$name" "$status"
      return 0
    fi

    for index in "${!USER_UNITS[@]}"; do
      states+=("$(printf '%s\n' "$output" | sed -n "$((index + 1))p")")
    done
  fi

  printf '%-7s' "$name"
  last_index=$((${#USER_UNITS[@]} - 1))
  for index in "${!USER_UNITS[@]}"; do
    state=${states[$index]:-unknown}
    field="${USER_UNITS[$index]}=${state}"

    if [ "$index" -eq "$last_index" ]; then
      printf ' %s' "$field"
    else
      printf ' %-28s' "$field"
    fi

    if [ "$state" != 'active' ]; then
      FAILED=1
    fi
  done
  printf '\n'
}

do_logs() {
  local name=$1
  local target=$2
  local unit
  local -a unit_args=()

  log "${name}: last ${LOG_LINES} lines of ${USER_UNITS[*]}"

  for unit in "${USER_UNITS[@]}"; do
    unit_args+=(-u "$unit")
  done

  if [ "$target" = '-' ]; then
    journalctl --user "${unit_args[@]}" -n "$LOG_LINES" --no-pager || FAILED=1
    return 0
  fi

  remote_run "$target" "$(
    user_env_snippet
    printf 'journalctl --user'
    for unit in "${USER_UNITS[@]}"; do
      printf ' -u %s' "$unit"
    done
    printf ' -n %s --no-pager\n' "$LOG_LINES"
  )" || FAILED=1
}

main() {
  local command=''
  local -a hosts=()
  local arg
  local entry
  local name
  local target

  while [ "$#" -gt 0 ]; do
    arg=$1
    shift

    case $arg in
      -h|--help)
        usage
        return 0
        ;;
      -n)
        if [ "$#" -eq 0 ]; then
          log 'option -n needs a line count'
          return 2
        fi
        LOG_LINES=$1
        shift
        ;;
      restart|start|stop|status|logs)
        if [ -n "$command" ]; then
          log "unexpected second command: ${arg}"
          usage
          return 2
        fi
        command=$arg
        ;;
      all)
        hosts=()
        for entry in "${HOSTS[@]}"; do
          hosts+=("${entry%%|*}")
        done
        ;;
      *)
        if ! known_host "$arg"; then
          log "unknown command or host: ${arg}"
          usage
          return 2
        fi
        hosts+=("$arg")
        ;;
    esac
  done

  if [ -z "$command" ]; then
    usage
    return 2
  fi

  if [ "${#hosts[@]}" -eq 0 ]; then
    for entry in "${HOSTS[@]}"; do
      hosts+=("${entry%%|*}")
    done
  fi

  for name in "${hosts[@]}"; do
    target=$(host_field "$name" 2)
    read -r -a USER_UNITS <<<"$(host_field "$name" 3)"

    case $command in
      restart|start|stop)
        do_lifecycle "$name" "$target" "$command"
        ;;
      status)
        do_status "$name" "$target"
        ;;
      logs)
        do_logs "$name" "$target"
        ;;
    esac
  done

  return "$FAILED"
}

main "$@"
