Consider adding coverage for:
- Root-owned state outside home, especially root.
- Service data in var (databases, mail spools, NetworkManager profiles, cron crontabs in cron).
- Machine-tracked job schedulers (systemd timers under timers, user crontabs).
- Optional: local CA/SSL material in ca-certificates (update-ca-certificates payload).
