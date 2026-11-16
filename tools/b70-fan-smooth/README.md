# b70-fan-smooth: optional calmer fan curve

Needs Intel's xe fan-control series 168027 (Karthik Poosa, https://patchwork.freedesktop.org/series/168027/; hwmon
`pwm1_enable`, `pwm1_auto_point*`). It keeps the stock curve but feeds it the package temperature averaged over 20 s,
and moves slowly: up <= 0.5 %/s, down <= 0.05 %/s. It writes a user table flat at the current speed below 80 C and
stock from 80 C up, so the firmware still ramps if the tool dies. At pkg >= 90 C it jumps to stock at once. On exit it
sets `pwm1_enable 2` (firmware curve); the unit runs `--stock` on stop too. `--dry-run` prints the speeds without writing.
Python 3 standard library only. Separate from the refresh fix; use either or both.

Install: `install -m755 b70-fan-smooth.py /usr/local/sbin/ && install -m644 b70-fan-smooth.service /etc/systemd/system/ && systemctl enable --now b70-fan-smooth`.
Test first: `sudo b70-fan-smooth.py --dry-run`. The fan-control series is not in this repo; build it into xe with
`../xe-oc-build.sh` and `XEOC_EXTRA=<series patches>`.

License: MIT (`../../LICENSE`).
