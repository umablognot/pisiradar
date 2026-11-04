#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pisi_stats.py — Pisi Linux paket güncellik istatistikleri (README otomatik tablosu)

Ne yapar?
  1. pisilinux/main ve pisilinux/core depolarını "blob:none" filtresiyle klonlar,
     yalnızca pspec.xml dosyalarını checkout eder.
  2. Her paketin pspec.xml'inden bakıcı (Packager), sürüm ve son çıkarma tarihini okur.
  3. Tek geçişlik "git log --name-only" çıktısından her paketin SON COMMIT tarihini
     çıkarır (blob indirmeden, tam geçmiş üzerinden).
  4. Son commit yaşına göre paketleri sınıflandırır:
        ≤ yeni_days (365)   -> YENİ      (yeşil)
        ≤ eski_days (730)   -> ESKİ KOD  (kırmızı)
        daha eski / tarih yok -> SORUNLU (bordo)
  5. Bakıcı bazında toplar ve README.md içindeki
        <!-- PISI-STATS:START --> ... <!-- PISI-STATS:END -->
     işaretçileri arasına shields.io rozetli markdown tabloyu yazar.

Kullanım:
    python3 scripts/pisi_stats.py --readme README.md
    python3 scripts/pisi_stats.py --readme README.md --init      # işaretçileri ilk kez ekler
    python3 scripts/pisi_stats.py --mode pspec                   # git geçmişi olmadan (hızlı/test)

Yalnızca Python standart kütüphanesi kullanır; ek kurulum gerekmez.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

# --------------------------------------------------------------------------
# Varsayılanlar (maintainers.json ile geçersiz kılınabilir)
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "repos": ["main", "core"],
    "org": "pisilinux",
    "attribution": "source",          # "source" = pspec'te bildirilen bakıcı, "last_update" = son güncelleyen
    "thresholds": {"yeni_days": 365, "eski_days": 730},
    "colors": {"yeni": "0A8109", "eski": "BB0909", "sorunlu": "833231"},
    "min_packages": 2,                # tabloda görünmesi için gereken en az paket sayısı
    "maintainers": {},                # "e-posta": {"name": "...", "handle": "github_kullanici_adi"},
    "aliases": {}                     # "birincil@eposta": ["ayni_kisi@baska.eposta", ...]
}

MARK_START = "<!-- PISI-STATS:START -->"
MARK_END = "<!-- PISI-STATS:END -->"
ORIGIN = "https://github.com/{org}/{repo}.git"

MISSING = object()


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, cwd=None, check=True, capture=False):
    """Alt süreç çalıştırıcı."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
    r = subprocess.run(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, errors="replace",
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"komut başarısız ({r.returncode}): {' '.join(cmd)}\n{(r.stderr or '')[:500]}")
    return r


def default_branch(url: str) -> str:
    """Uzak deponun varsayılan dalını bul (master/main/...)."""
    r = run(["git", "ls-remote", "--symref", url, "HEAD"], capture=True)
    for line in r.stdout.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].rsplit("/", 1)[-1]
    return "master"


def sparse_checkout(repo_dir: str, branch: str) -> None:
    """Yalnızca pspec.xml dosyalarını çek (blob tasarrufu)."""
    try:
        run(["git", "-C", repo_dir, "sparse-checkout", "init", "--no-cone"])
        run(["git", "-C", repo_dir, "sparse-checkout", "set", "pspec.xml"])
        run(["git", "-C", repo_dir, "checkout", branch])
    except RuntimeError:
        log("  ! sparse-checkout başarısız, tam checkout deneniyor...")
        run(["git", "-C", repo_dir, "checkout", branch])


def worktree_has_pspec(repo_dir: str) -> bool:
    for _, _, files in os.walk(repo_dir):
        if "pspec.xml" in files:
            return True
    return False


def clone_repo(url: str, dest: str, reuse: bool) -> str:
    """Filtreli klon: geçmiş geliyor, blob'lar gelmiyor. Yalnız pspec.xml checkout edilir."""
    if reuse and os.path.isdir(dest):
        if worktree_has_pspec(dest):
            log(f"  ↺ mevcut klon kullanılıyor: {dest}")
            return dest
        log(f"  ↺ klon var ama çalışma ağacı boş; pspec.xml'ler çekiliyor...")
        r = run(["git", "-C", dest, "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture=True, check=False)
        branch = (r.stdout or "").strip().rsplit("/", 1)[-1] or default_branch(url)
        sparse_checkout(dest, branch)
        return dest
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    branch = default_branch(url)
    log(f"  ⬇  klonlanıyor (filtreli, dal: {branch}): {url}")
    run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
         "--single-branch", "--branch", branch, url, dest])
    sparse_checkout(dest, branch)
    return dest


# --------------------------------------------------------------------------
# pspec.xml ayrıştırma
# --------------------------------------------------------------------------
def parse_pspec(path: str):
    """pspec.xml -> paket adı, sürüm, kaynak bakıcı, son güncelleme tarihi/güncelleyeni."""
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    src = root.find("Source")
    if src is None:
        return None
    name = (src.findtext("Name") or "").strip()
    sp = src.find("Packager")
    src_mail = (sp.findtext("Email") or "").strip().lower() if sp is not None else ""
    src_name = (sp.findtext("Name") or "").strip() if sp is not None else ""

    version, last_date, last_mail, last_name = None, None, "", ""
    hist = root.find("History")
    if hist is not None:
        upd = hist.find("Update")
        if upd is not None:
            version = (upd.findtext("Version") or "").strip() or None
            d = (upd.findtext("Date") or "").strip()
            try:
                last_date = sane_dt(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc))
            except ValueError:
                last_date = None
            up = upd.find("Packager")
            if up is not None:
                last_mail = (up.findtext("Email") or "").strip().lower()
                last_name = (up.findtext("Name") or "").strip()
    return {
        "name": name, "version": version,
        "src_email": src_mail, "src_name": src_name,
        "update_date": last_date, "update_email": last_mail, "update_name": last_name,
    }


def collect_packages(repo_dir: str, repo: str):
    """Çalışma ağacındaki tüm pspec.xml'leri oku."""
    packages = []
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        if "pspec.xml" in filenames:
            rel = os.path.relpath(dirpath, repo_dir).replace(os.sep, "/")
            info = parse_pspec(os.path.join(dirpath, "pspec.xml"))
            if info:
                info["repo"] = repo
                info["path"] = rel
                packages.append(info)
    return packages


def last_commit_dates(repo_dir: str, package_roots: set) -> dict:
    """Tek geçişlik git log: paket kökü -> son commit ISO tarihi."""
    dates = {}
    cache = {}

    def find_root(path: str):
        parts = path.split("/")
        for i in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:i])
            if cand in package_roots:
                return cand
        return None

    # -m --first-parent: yalnız ana hat gezilir; merge commit'ler PR'daki tüm
    # dosya değişikliklerini tek seferde listeler -> çok daha hızlı.
    proc = subprocess.Popen(
        ["git", "-C", repo_dir, "log", "-m", "--first-parent",
         "--format=@%cI", "--name-only"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
    )
    cur = None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("@"):
            cur = line[1:]
        elif line and cur:
            root = cache.get(line, MISSING)
            if root is MISSING:
                root = find_root(line)
                cache[line] = root
            if root and (root not in dates or cur > dates[root]):
                dates[root] = cur
    proc.wait()
    return dates


# --------------------------------------------------------------------------
# Sınıflandırma ve toplama
# --------------------------------------------------------------------------
def classify(dt, thresholds) -> str:
    if dt is None:
        return "sorunlu"
    now = datetime.now(timezone.utc)
    age_days = (now - dt).days
    if age_days <= thresholds["yeni_days"]:
        return "yeni"
    if age_days <= thresholds["eski_days"]:
        return "eski"
    return "sorunlu"


def iso_to_dt(s: str):
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return sane_dt(dt)


def auto_seed(by_email: dict):
    """Bulunan e-postalar için maintainers + aliases tohumu üretir.

    Kural 1: adında "community"/"admins" geçenler ve e-postası bozuk olanlar
             -> admins@pisilinux.org altında birleştir.
    Kural 2: aynı (büyük/küçük harf duyarsız) isme sahip e-postalar -> geçerli
             biçimli ve en çok paketli e-posta birincil yapılır, diğerleri takma ad.
    """
    import re
    COMMUNITY = "admins@pisilinux.org"
    VALID = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

    def norm(nm: str) -> str:
        return " ".join(nm.casefold().split())

    community, pending, groups = [], [], {}
    for email, m in by_email.items():
        nm = norm(m["name"])
        if "community" in nm or "admins" in nm:
            community.append(email)
        elif not VALID.match(email):
            pending.append((nm, email))
        else:
            groups.setdefault(nm, []).append(email)
    # Bozuk e-postalı kişi geçerli bir grupla eşleşiyorsa ona kat, yoksa topluluğa ekle
    for nm, email in pending:
        (groups[nm] if nm in groups else community).append(email)

    maintainers, aliases = {}, {}

    def register(primary: str, alts: list, display: str):
        maintainers[primary] = {"name": display, "handle": ""}
        alts = [a for a in alts if a != primary]
        if alts:
            aliases[primary] = sorted(alts)

    if community:
        register(COMMUNITY, community, "PisiLinux Community")

    for _, emails in groups.items():
        primary = max(emails, key=lambda e: (bool(VALID.match(e)), by_email[e]["total"]))
        register(primary, [e for e in emails if e != primary],
                 by_email[primary]["name"])

    maintainers = dict(sorted(maintainers.items(),
                              key=lambda kv: -by_email[kv[0]]["total"]))
    return maintainers, aliases


def sane_dt(dt):
    """Geleceğe düşen hatalı tarihleri (ör. 3020!) eler."""
    if dt is None:
        return None
    return dt if dt <= datetime.now(timezone.utc) else None


def build_stats(packages, cfg):
    """Paket listesini bakıcı bazında topla (takma adlar birleştirilerek)."""
    thresholds = cfg["thresholds"]
    alias = {}
    for primary, alts in (cfg.get("aliases") or {}).items():
        alias[primary.strip().lower()] = primary.strip().lower()
        for a in alts:
            alias[a.strip().lower()] = primary.strip().lower()
    by_email = {}
    for p in packages:
        if cfg["attribution"] == "last_update" and p.get("update_email"):
            raw = p["update_email"]
            disp = p.get("update_name") or raw
        else:
            raw = p["src_email"]
            disp = p.get("src_name") or raw
        email = alias.get((raw or "?").strip().lower(), (raw or "?").strip().lower())
        m = by_email.setdefault(email, {
            "name": disp, "total": 0, "yeni": 0, "eski": 0, "sorunlu": 0,
            "repos": {}, "last_active": None, "packages": [],
        })
        m["total"] += 1
        m["repos"][p["repo"]] = m["repos"].get(p["repo"], 0) + 1
        status = classify(p["fresh_dt"], thresholds)
        p["status"] = status
        m[status] += 1
        if p["fresh_dt"] and (m["last_active"] is None or p["fresh_dt"] > m["last_active"]):
            m["last_active"] = p["fresh_dt"]
        m["packages"].append(p)
    return by_email


# --------------------------------------------------------------------------
# Markdown üretimi
# --------------------------------------------------------------------------
def _esc_badge(text: str) -> str:
    # shields.io kaçış kuralları: '-' -> '--', '_' -> '__', boşluk -> '_'
    t = (text.replace("-", "--").replace("_", "__").replace(" ", "_"))
    return quote(t, safe="-.")


def badge(label: str, message: str, color: str) -> str:
    return (f'<img src="https://img.shields.io/badge/'
            f'{_esc_badge(label)}-{_esc_badge(message)}-{color}" '
            f'alt="{label} {message}">')


def pct(n: int, total: int) -> str:
    """Türk biçemi: %65, %53.7 (uç sıfır yok)."""
    if not total:
        return "%0"
    v = n * 100.0 / total
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"%{s}"


def generate_markdown(by_email, cfg, generated_at) -> str:
    colors = cfg["colors"]
    rows = [m for m in by_email.values() if m["total"] >= cfg["min_packages"]]
    rows.sort(key=lambda m: (-m["total"], -m["yeni"], m["name"].lower()))

    lines = []
    t = cfg["thresholds"]
    lines.append(f"**Lejant:** {badge('Yeni', '≤ ' + str(t['yeni_days']) + ' gün', colors['yeni'])} "
                 f"son {t['yeni_days']} gün içinde güncellenmiş · "
                 f"{badge('Eski Kod', str(t['yeni_days']) + '–' + str(t['eski_days']) + ' gün', colors['eski'])} · "
                 f"{badge('Sorunlu', '> ' + str(t['eski_days']) + ' gün', colors['sorunlu'])}  ")
    lines.append(f"*Son güncelleme: {generated_at}*")
    lines.append("")
    lines.append("| Maintainer | Etkin Kullanıcı | Toplam | Yeni | Eski Kod | Sorunlu | Tüm Repolar | Yıl |")
    lines.append("|---|:---:|---:|:---:|:---:|:---:|:---:|:---:|")

    def row(name, handle, m):
        link_name = f"[{name}](https://github.com/{handle})" if handle else name
        user = f"[@{handle}]({'https://github.com/' + handle})" if handle else "—"
        total = m["total"]
        yeni_b = badge("Yeni", pct(m["yeni"], total), colors["yeni"])
        eski_b = badge("Eski", str(m["eski"]), colors["eski"]) if m["eski"] else str(m["eski"])
        sor_b = badge("Sorunlu", str(m["sorunlu"]), colors["sorunlu"]) if m["sorunlu"] else str(m["sorunlu"])
        repos = " · ".join(
            f"[{r}](https://github.com/{cfg['org']}/{r}) **{c}**"
            for r, c in sorted(m["repos"].items(), key=lambda kv: -kv[1])
        )
        year = m["last_active"].year if m["last_active"] else "—"
        return (f"| {link_name} | {user} | **{total}** | {yeni_b} | {eski_b} | {sor_b} "
                f"| {repos} | {year} |")

    for m in rows:
        handle = cfg["maintainers"].get(m["_email"], {}).get("handle", "")
        lines.append(row(m["name"], handle, m))

    # Genel toplam satırı — TÜM bakıcılar (min_packages altındakiler dahil)
    all_m = list(by_email.values())
    g = {"total": sum(m["total"] for m in all_m), "yeni": sum(m["yeni"] for m in all_m),
         "eski": sum(m["eski"] for m in all_m), "sorunlu": sum(m["sorunlu"] for m in all_m),
         "repos": {}, "last_active": None}
    for m in all_m:
        for r, c in m["repos"].items():
            g["repos"][r] = g["repos"].get(r, 0) + c
        if m["last_active"] and (g["last_active"] is None or m["last_active"] > g["last_active"]):
            g["last_active"] = m["last_active"]
    lines.append(row("**GENEL TOPLAM**", "", g))
    return "\n".join(lines)


def splice_readme(readme_path: str, block: str, init: bool) -> bool:
    """İşaretçiler arasını değiştir. İşaretçi yoksa: --init ile bölüm ekle, yoksa uyar."""
    if os.path.exists(readme_path):
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
    else:
        content = ""

    if MARK_START in content and MARK_END in content:
        pre, rest = content.split(MARK_START, 1)
        _, post = rest.split(MARK_END, 1)
        new = f"{pre}{MARK_START}\n{block}\n{MARK_END}{post}"
    elif init or not os.path.exists(readme_path):
        section = f"\n## 📦 Paket Güncellik Panosu\n\n{MARK_START}\n{block}\n{MARK_END}\n"
        new = content.rstrip("\n") + section if content else section.lstrip("\n")
    else:
        log(f"! {readme_path} içinde {MARK_START} ... {MARK_END} işaretçileri yok.")
        log("  Eklemek için: python3 scripts/pisi_stats.py --readme README.md --init")
        return False

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new)
    return True


# --------------------------------------------------------------------------
# Ana akış
# --------------------------------------------------------------------------
def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # derin kopya
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "maintainers" and isinstance(v, dict):
                cfg["maintainers"].update({k2.lower(): v2 for k2, v2 in v.items()})
            else:
                cfg[k] = v
    else:
        log(f"• {path} yok, varsayılan ayarlar kullanılacak.")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="Pisi Linux güncellik istatistikleri")
    ap.add_argument("--config", default="maintainers.json")
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--out", default="stats.json")
    ap.add_argument("--mode", choices=["git", "pspec", "auto"], default="auto",
                    help="git: tam geçmişten son commit tarihi; pspec: yalnız pspec tarihi")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--reuse", action="store_true", help="mevcut klonları yeniden kullan")
    ap.add_argument("--init", action="store_true", help="README'de işaretçi bölümü yoksa oluştur")
    args = ap.parse_args()

    cfg = load_config(args.config)
    generated_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    workdir = args.workdir or tempfile.mkdtemp(prefix="pisi-stats-")
    os.makedirs(workdir, exist_ok=True)

    packages = []
    mode_used = args.mode if args.mode != "auto" else "git"
    for repo in cfg["repos"]:
        url = ORIGIN.format(org=cfg["org"], repo=repo)
        log(f"▸ Depo: {repo}")
        try:
            repo_dir = clone_repo(url, os.path.join(workdir, repo), args.reuse)
        except RuntimeError as e:
            log(f"  ! klonlama başarısız, depo atlanıyor: {e}")
            continue
        pkgs = collect_packages(repo_dir, repo)
        log(f"  ✓ {len(pkgs)} pspec.xml okundu")

        if args.mode in ("git", "auto"):
            roots = {p["path"] for p in pkgs}
            try:
                log("  ⏳ son commit tarihleri çıkarılıyor (tek geçiş, blob indirmeden)...")
                dates = last_commit_dates(repo_dir, roots)
                log(f"  ✓ {len(dates)} paket için commit tarihi bulundu")
                for p in pkgs:
                    g = iso_to_dt(dates.get(p["path"]))
                    ps = p.get("update_date")
                    p["fresh_dt"] = max(filter(None, [g, ps]), default=None)
                mode_used = "git"
            except RuntimeError as e:
                if args.mode == "git":
                    raise
                log(f"  ! git log başarısız, pspec tarihlerine dönülüyor: {e}")
                for p in pkgs:
                    p["fresh_dt"] = p.get("update_date")
                mode_used = "pspec"
        else:
            for p in pkgs:
                p["fresh_dt"] = p.get("update_date")

        packages.extend(pkgs)

    if not packages:
        log("! Hiç paket okunamadı; README güncellenmedi.")
        return 1

    log(f"▸ Toplam {len(packages)} paket, "
        f"{len(set(p['src_email'] for p in packages))} bakıcı e-postası")
    by_email = build_stats(packages, cfg)
    for email, m in by_email.items():
        m["_email"] = email

    # Yapılandırma dosyası yoksa bulunan bakıcılarla tohumla (var olanın üstüne yazmaz)
    if not os.path.exists(args.config):
        seed = json.loads(json.dumps(DEFAULT_CONFIG))
        seed["maintainers"], seed["aliases"] = auto_seed(by_email)
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(seed, f, ensure_ascii=False, indent=2)
        log(f"• {args.config} oluşturuldu ({len(seed['maintainers'])} bakıcı, "
            f"{len(seed['aliases'])} takma ad grubu); GitHub handle'larını "
            "(\"handle\") düzenleyerek @bağlantıları açabilirsiniz.")
        # İlk tablo da birleşmiş veriyle kuralsın:
        cfg["maintainers"], cfg["aliases"] = seed["maintainers"], seed["aliases"]
        by_email = build_stats(packages, cfg)
        for email, m in by_email.items():
            m["_email"] = email

    block = generate_markdown(by_email, cfg, generated_at)
    ok = splice_readme(args.readme, block, args.init)

    stats = {
        "generated_at": generated_at,
        "mode": mode_used,
        "repos": cfg["repos"],
        "packages_total": len(packages),
        "maintainers": {
            email: {k: (v.date().isoformat() if isinstance(v, datetime) else v)
                    for k, v in m.items() if k not in ("packages", "_email")}
            for email, m in by_email.items()
        },
        "packages": [
            {"repo": p["repo"], "path": p["path"], "name": p["name"], "version": p["version"],
             "maintainer": p["src_email"],
             "last_commit": (p["fresh_dt"].date().isoformat() if p.get("fresh_dt") else None),
             "status": p["status"]}
            for p in packages
        ],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    top = sorted(by_email.values(), key=lambda m: -m["total"])[:5]
    log("▸ İlk 5 bakıcı:")
    for m in top:
        log(f"    {m['name']:<30} {m['total']:>5} paket · {pct(m['yeni'], m['total']):>6} yeni")
    if not args.reuse:
        shutil.rmtree(workdir, ignore_errors=True)
    log(f"▸ Bitti. README güncellendi: {ok}, istatistikler: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
