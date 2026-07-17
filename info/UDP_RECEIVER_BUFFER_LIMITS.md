# UDP receive buffers (first-time Linux setup)

When you deploy **splash_timepix** with **Serval** and the ASI detector stack, one machine (often the acquisition PC) receives high-rate detector traffic over the network. Serval is configured to use a **large UDP receive buffer** (on the order of tens of MB) so short bursts and normal scheduling jitter do not overflow a tiny kernel queue.

On many Linux installs, the kernel’s global ceiling for socket receive buffers (`net.core.rmem_max`) is still at a small default. Applications can *ask* for a large buffer, but the kernel will **silently cap** the granted size at that ceiling. **Raising `rmem_max` and `rmem_default` once on the streaming host ensures the buffer Serval expects is actually granted**, which avoids unnecessary packet loss under burst traffic and normal scheduling jitter.

**Do this on the host that receives the detector UDP stream** (or on any machine where you run Serval and care about stable streaming), ideally **before** your first long run or production tuning.

---

## 1. Apply for this session (immediate)

```bash
sudo sysctl -w net.core.rmem_max=26214400
sudo sysctl -w net.core.rmem_default=26214400
```

These values are in bytes (here **25 MiB**). They apply immediately; they are **not** kept across reboot.

---

## 2. Make the settings persistent

Prefer a **drop-in file** under `/etc/sysctl.d/` instead of editing `/etc/sysctl.conf`. Drop-ins are easy to copy between machines, version in config management, or remove.

```bash
sudo tee /etc/sysctl.d/99-serval.conf > /dev/null <<'EOF'
# Serval / detector UDP receive buffer tuning (25 MiB ceiling)
net.core.rmem_max=26214400
net.core.rmem_default=26214400
EOF
```

The `99-` prefix loads after most defaults so these values win when order matters.

---

## 3. Load all sysctl files

```bash
sudo sysctl --system
```

This applies configuration from `/etc/sysctl.conf`, `/etc/sysctl.d/*.conf`, `/run/sysctl.d/*.conf`, and `/usr/lib/sysctl.d/*.conf`.

---

## 4. Confirm

```bash
sysctl net.core.rmem_max net.core.rmem_default
```

Expected:

```
net.core.rmem_max = 26214400
net.core.rmem_default = 26214400
```
