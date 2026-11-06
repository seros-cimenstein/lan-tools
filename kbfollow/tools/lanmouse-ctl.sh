#!/usr/bin/env bash
set -u

# name|ssh_target(- = local)|system_unit|indicator
# indicator is a user unit name, or `swaybar` when lanmouse-indicator.py runs
# as swaybar's status_command (swaybar owns that process, so it is only checked).
HOSTS=(
  'laptop|-|lanmouse-sender|swaybar'
  'laptop|laptop|lanmouse-receiver|lanmouse-indicator'
  'desktop|desktop|lanmouse-receiver|lanmouse-indicator'
)

# The [r] keeps pgrep from matching a shell whose command line contains this text.
SWAYBAR_PATTERN='lanmouse-indicator.py --swayba[r]'
LOG_LINES=40
SSH_OPTS=(
  -o ConnectTimeout=5
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
)

FAILED=0

usage() {
  cat <<'USAGE'
Usage: lanmouse-ctl.sh <command> [host...] [-n LINES]

Commands:
  restart   restart the lanmouse units
  start     start the lanmouse units
  stop      stop the lanmouse units
  status    print is-active for each unit
  logs      print recent journal lines (-n LINES, default 40)

Hosts:
  laptop    this machine (lanmouse-sender + indicator in swaybar)
  laptop      ssh laptop (lanmouse-receiver + user lanmouse-indicator)
  desktop     ssh desktop (lanmouse-receiver + user lanmouse-indicator)
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

# Same user-session preparation as tools/desktop-rescue.sh, so `systemctl --user`
# works over a non-login ssh connection.
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
# than piped to `bash -s` so that `ssh -t` leaves the tty free for sudo prompts.
remote_run() {
  local target=$1
  local want_tty=$2
  local snippet=$3
  local status

  # Only ask for a remote tty when we have one to forward; otherwise ssh warns
  # and sudo fails with a less obvious message.
  if [ "$want_tty" = 'tty' ] && [ -t 0 ]; then
    ssh -t "${SSH_OPTS[@]}" "$target" "$snippet"
  else
    ssh "${SSH_OPTS[@]}" "$target" "$snippet"
  fi
  status=$?

  if [ "$status" -eq 255 ]; then
    log "unreachable (ssh exit ${status})"
  elif [ "$status" -ne 0 ]; then
    log "command failed with exit ${status}"
  fi

  return "$status"
}

# Shell command printing the indicator's state as a single is-active word.
indicator_state_cmd() {
  if [ "$1" = 'swaybar' ]; then
    printf "if pgrep -f -- '%s' >/dev/null; then echo active; else echo inactive; fi\n" \
      "$SWAYBAR_PATTERN"
  else
    printf 'systemctl --user is-active %s\n' "$1"
  fi
}

do_lifecycle() {
  local name=$1
  local target=$2
  local unit=$3
  local indicator=$4
  local action=$5
  local status

  if [ "$indicator" = 'swaybar' ]; then
    log "${name}: ${action} ${unit} (indicator runs inside swaybar; not touched)"
  else
    log "${name}: ${action} ${unit} + ${indicator}"
  fi

  if [ "$target" = '-' ]; then
    sudo systemctl "$action" "$unit"
    status=$?
    if [ "$status" -ne 0 ]; then
      log "${name}: sudo systemctl ${action} ${unit} failed with exit ${status}"
      FAILED=1
    fi

    if [ "$indicator" != 'swaybar' ]; then
      systemctl --user "$action" "$indicator"
      status=$?
      if [ "$status" -ne 0 ]; then
        log "${name}: systemctl --user ${action} ${indicator} failed with exit ${status}"
        FAILED=1
      fi
    fi

    return 0
  fi

  remote_run "$target" tty "$(
    user_env_snippet
    printf 'rc=0\n'
    printf 'sudo systemctl %s %s || rc=$?\n' "$action" "$unit"
    if [ "$indicator" != 'swaybar' ]; then
      printf 'systemctl --user %s %s || rc=$?\n' "$action" "$indicator"
    fi
    printf 'exit "$rc"\n'
  )" || FAILED=1
}

do_status() {
  local name=$1
  local target=$2
  local unit=$3
  local indicator=$4
  local system_state
  local user_state
  local output
  local status

  if [ "$target" = '-' ]; then
    system_state=$(systemctl is-active "$unit" 2>&1)
    user_state=$(eval "$(indicator_state_cmd "$indicator")" 2>&1)
  else
    # `is-active` exits non-zero for inactive units, so only ssh's own 255
    # counts as unreachable here.
    output=$(ssh "${SSH_OPTS[@]}" "$target" "$(
      user_env_snippet
      printf 'systemctl is-active %s\n' "$unit"
      indicator_state_cmd "$indicator"
    )" 2>/dev/null)
    status=$?
    if [ "$status" -eq 255 ] || [ -z "$output" ]; then
      FAILED=1
      printf '%-7s unreachable (ssh exit %s)\n' "$name" "$status"
      return 0
    fi
    system_state=$(printf '%s\n' "$output" | sed -n '1p')
    user_state=$(printf '%s\n' "$output" | sed -n '2p')
  fi

  printf '%-7s %-28s %s\n' \
    "$name" "${unit}=${system_state:-unknown}" "${indicator}=${user_state:-unknown}"

  if [ "$system_state" != 'active' ] || [ "$user_state" != 'active' ]; then
    FAILED=1
  fi
}

do_logs() {
  local name=$1
  local target=$2
  local unit=$3
  local indicator=$4

  if [ "$indicator" = 'swaybar' ]; then
    log "${name}: last ${LOG_LINES} lines of ${unit} (swaybar indicator has no journal)"
  else
    log "${name}: last ${LOG_LINES} lines of ${unit} and ${indicator}"
  fi

  if [ "$target" = '-' ]; then
    sudo journalctl -u "$unit" -n "$LOG_LINES" --no-pager || FAILED=1
    if [ "$indicator" != 'swaybar' ]; then
      journalctl --user -u "$indicator" -n "$LOG_LINES" --no-pager || FAILED=1
    fi
    return 0
  fi

  remote_run "$target" tty "$(
    user_env_snippet
    printf 'rc=0\n'
    printf 'sudo journalctl -u %s -n %s --no-pager || rc=$?\n' "$unit" "$LOG_LINES"
    if [ "$indicator" != 'swaybar' ]; then
      printf 'journalctl --user -u %s -n %s --no-pager || rc=$?\n' "$indicator" "$LOG_LINES"
    fi
    printf 'exit "$rc"\n'
  )" || FAILED=1
}

main() {
  local command=''
  local -a hosts=()
  local arg
  local entry
  local name
  local target
  local unit
  local indicator

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
    unit=$(host_field "$name" 3)
    indicator=$(host_field "$name" 4)

    case $command in
      restart|start|stop)
        do_lifecycle "$name" "$target" "$unit" "$indicator" "$command"
        ;;
      status)
        do_status "$name" "$target" "$unit" "$indicator"
        ;;
      logs)
        do_logs "$name" "$target" "$unit" "$indicator"
        ;;
    esac
  done

  return "$FAILED"
}

main "$@"
