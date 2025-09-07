#!/usr/bin/env bash
set -euo pipefail
cd /home/satisfactory/satisfactory_bots

set -a; . ./.env; set +a

: "${RCLONE_REMOTE:?RCLONE_REMOTE fehlt in .env}"
: "${RCLONE_BACKUP_PATH:?RCLONE_BACKUP_PATH fehlt in .env}"
: "${LOCAL_SAVE_DIR:?LOCAL_SAVE_DIR fehlt in .env}"

TS="$(date +%F_%H%M)"
HOST="$(hostname -s)"
DEST="${RCLONE_REMOTE}:${RCLONE_BACKUP_PATH}/${TS}-${HOST}"
LOGDIR="/home/satisfactory/satisfactory_bots/logs"; mkdir -p "$LOGDIR"

echo "Starte Backup: $(date)"
echo "Quelle: $LOCAL_SAVE_DIR"
echo "Ziel: $DEST"

rclone copy "$LOCAL_SAVE_DIR" "$DEST" \
  --create-empty-src-dirs --copy-links --fast-list \
  --transfers 8 --checkers 16 \
  --log-file "$LOGDIR/backup_${TS}.log" --log-level INFO

if [[ -n "${MAX_BACKUPS:-}" ]]; then
  mapfile -t dirs < <(rclone lsf "${RCLONE_REMOTE}:${RCLONE_BACKUP_PATH}" --dirs-only | sed 's:/$::' | sort)
  cnt=${#dirs[@]}
  if (( cnt > MAX_BACKUPS )); then
    echo "Lösche alte Backups..."
    for d in "${dirs[@]:0:cnt-MAX_BACKUPS}"; do
      echo "Lösche: $d"
      rclone purge "${RCLONE_REMOTE}:${RCLONE_BACKUP_PATH}/${d}" || true
    done
  fi
fi

echo "[OK] Backup nach $DEST abgeschlossen: $(date)"