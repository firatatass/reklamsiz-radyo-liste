#!/usr/bin/env python3
"""M3U listesindeki yayınları test eder ve çalışmayanları raporlar.

Her yayın için uygulamanın yaptığını taklit eder:
  HLS (.m3u8): liste -> (varsa) en düşük kaliteli varyant -> son video/ses parçası indirilir.
  DASH (.mpd): manifest indirilebiliyor mu bakılır.
  Diğer (radyo akışı vb.): akıştan en az 16 KB veri okunabiliyor mu bakılır.

Geçici hataları elemek için başarısız yayınlar birkaç tur daha denenir.
Sadece Python standart kütüphanesini kullanır.

Kullanım:
  python kontrol.py LISTE.m3u [--rapor rapor.md] [--json sonuc.json] [--tur 3] [--paralel 16]
Çıkış kodu: 0 = hepsi çalışıyor, 1 = çalışmayan yayın var.
"""
import argparse
import concurrent.futures
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import gzip
import zlib

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 15
TEKRAR_TIMEOUT = 30  # tekrar turlarında yavaş sunuculara daha uzun süre tanınır
MIN_SEGMENT_BYTES = 10_000
MIN_STREAM_BYTES = 16_000

# Bu HTTP kodları çoğunlukla yayının yurt dışına kapalı olduğunu gösterir (GitHub sunucuları ABD'de).
GEO_CODES = {401, 403, 451}

SSL_CTX = ssl.create_default_context()
# Android'in desteklediği eski (RSA anahtar değişimli) şifreleme yöntemlerine de izin ver; sertifika denetimi aynen sürer.
SSL_CTX.set_ciphers("DEFAULT")
TIMEOUT_SIMDI = [TIMEOUT]


class YayinHatasi(Exception):
    def __init__(self, mesaj, http_kodu=None):
        super().__init__(mesaj)
        self.http_kodu = http_kodu


def uygulama_gibi_duzelt(url):
    """Uygulamanın (RadioService.playRadio) çalmadan önce linke yaptığı düzeltmelerin aynısı."""
    u = url.strip().replace("?checkedby:iptvcat.com", "").replace("&checkedby:iptvcat.com", "")
    if u.lower().endswith("/listen.pls"):
        u = u[: -len("/listen.pls")]
    elif u.lower().endswith(".pls"):
        u = u[:-4]
    if re.match(r"(?i)^https?://[^/:]+:[0-9]+/?$", u):
        u += ";" if u.endswith("/") else "/;"
    return u


def icy_oku(url, limit):
    """Standart dışı 'ICY 200 OK' cevabı veren eski SHOUTcast sunucularını okur (uygulama bunları çalabiliyor)."""
    p = urllib.parse.urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    yol = (p.path or "/") + ("?" + p.query if p.query else "")
    s = socket.create_connection((p.hostname, port), timeout=TIMEOUT_SIMDI[0])
    try:
        if p.scheme == "https":
            s = SSL_CTX.wrap_socket(s, server_hostname=p.hostname)
        s.sendall((f"GET {yol} HTTP/1.0\r\nHost: {p.hostname}\r\nUser-Agent: {USER_AGENT}\r\n"
                   "Icy-MetaData: 1\r\nConnection: close\r\n\r\n").encode())
        veri = b""
        while len(veri) < limit + 4096:
            p2 = s.recv(65536)
            if not p2:
                break
            veri += p2
    finally:
        s.close()
    bas, _, govde = veri.partition(b"\r\n\r\n")
    durum = bas.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    m = re.match(r"\S+\s+(\d{3})", durum)
    kod = int(m.group(1)) if m else 0
    if kod != 200:
        raise YayinHatasi(f"HTTP {kod or durum[:30]}", kod or None)
    return govde, url


def oku(url, limit=None, radyo=False):
    """URL'yi indirir. (gövde, son_url) döner. limit verilirse en fazla o kadar bayt okur."""
    basliklar = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if radyo:
        basliklar = {"User-Agent": USER_AGENT, "Icy-MetaData": "1"}  # uygulama radyolarda bunu gönderiyor
    istek = urllib.request.Request(url, headers=basliklar)
    try:
        with urllib.request.urlopen(istek, timeout=TIMEOUT_SIMDI[0], context=SSL_CTX) as yanit:
            son_url = yanit.geturl()
            kodlama = (yanit.headers.get("Content-Encoding") or "").lower()
            if limit is not None and not kodlama:
                parcalar, toplam = [], 0
                while toplam < limit:
                    p = yanit.read(min(65536, limit - toplam))
                    if not p:
                        break
                    parcalar.append(p)
                    toplam += len(p)
                return b"".join(parcalar), son_url
            govde = yanit.read()
            if "gzip" in kodlama:
                govde = gzip.decompress(govde)
            elif "deflate" in kodlama:
                govde = zlib.decompress(govde)
            return govde, son_url
    except urllib.error.HTTPError as e:
        raise YayinHatasi(f"HTTP {e.code}", e.code)
    except urllib.error.URLError as e:
        raise YayinHatasi(f"bağlantı hatası: {e.reason}")
    except (socket.timeout, TimeoutError):
        raise YayinHatasi("zaman aşımı")
    except Exception as e:  # noqa: BLE001
        if "ICY" in str(e) and limit is not None:
            try:
                return icy_oku(url, limit)
            except YayinHatasi:
                raise
            except Exception as e2:  # noqa: BLE001
                raise YayinHatasi(f"hata: {type(e2).__name__}: {e2}")
        raise YayinHatasi(f"hata: {type(e).__name__}: {e}")


def en_dusuk_varyant(master, taban_url):
    satirlar = master.splitlines()
    adaylar = []
    for i, satir in enumerate(satirlar):
        if satir.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", satir)
            bant = int(m.group(1)) if m else 10**12
            for sonraki in satirlar[i + 1:]:
                sonraki = sonraki.strip()
                if sonraki and not sonraki.startswith("#"):
                    adaylar.append((bant, urllib.parse.urljoin(taban_url, sonraki)))
                    break
    adaylar.sort()
    return [u for _, u in adaylar]


def hls_test(govde, son_url):
    metin = govde.decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in metin:
        son_hata = None
        for varyant in en_dusuk_varyant(metin, son_url)[:3]:
            try:
                v_govde, v_url = oku(varyant)
                return parca_test(v_govde.decode("utf-8", "replace"), v_url)
            except YayinHatasi as e:
                son_hata = e
        raise son_hata or YayinHatasi("varyant bulunamadı")
    return parca_test(metin, son_url)


def parca_test(metin, taban_url):
    if "#EXTM3U" not in metin:
        raise YayinHatasi("geçersiz yayın listesi")
    parcalar = [s.strip() for s in metin.splitlines() if s.strip() and not s.startswith("#")]
    if not parcalar:
        raise YayinHatasi("yayın listesinde parça yok")
    parca_url = urllib.parse.urljoin(taban_url, parcalar[-1])
    veri, _ = oku(parca_url, limit=2_000_000)
    if len(veri) < MIN_SEGMENT_BYTES:
        raise YayinHatasi(f"parça çok küçük ({len(veri)} bayt)")
    return "HLS"


def yayin_test(url):
    url = uygulama_gibi_duzelt(url)
    hls_veya_dash = bool(re.search(r"\.(m3u8|mpd)", url, re.I))
    govde, son_url = oku(url, limit=None if hls_veya_dash else MIN_STREAM_BYTES, radyo=not hls_veya_dash)
    bas = govde[:4096].lstrip()
    if bas.startswith(b"#EXTM3U") or re.search(r"\.m3u8", url, re.I):
        return hls_test(govde, son_url)
    if b"<MPD" in govde[:4096] or re.search(r"\.mpd", url, re.I):
        if b"<MPD" not in govde:
            raise YayinHatasi("geçersiz DASH manifesti")
        return "DASH"
    if len(govde) < MIN_STREAM_BYTES:
        raise YayinHatasi(f"akıştan yeterli veri gelmedi ({len(govde)} bayt)")
    return "akış"


def m3u_oku(yol):
    kanallar, ad, grup = [], None, ""
    with open(yol, encoding="utf-8") as f:
        for satir in f:
            satir = satir.strip()
            if satir.upper().startswith("#EXTINF"):
                grup_m = re.search(r'group-title="([^"]*)"', satir)
                grup = grup_m.group(1) if grup_m else ""
                ad = satir.rsplit('"', 1)[-1].split(",", 1)[-1].strip() if '"' in satir else satir.split(",", 1)[-1].strip()
            elif satir.lower().startswith("http"):
                kanallar.append({"ad": ad or "Bilinmeyen", "grup": grup, "url": satir})
                ad, grup = None, ""
    return kanallar


YURTDISI_ENGELLI = set()


def yurtdisi_listesi_oku(yol):
    """Türkiye'den çalışan ama yurt dışına farklı hata koduyla kapalı olduğu bilinen yayınlar (her satıra bir link)."""
    try:
        with open(yol, encoding="utf-8") as f:
            return {s.strip() for s in f if s.strip() and not s.strip().startswith("#")}
    except FileNotFoundError:
        return set()


def tek_test(kanal):
    bas = time.time()
    try:
        tur = yayin_test(kanal["url"])
        return {**kanal, "durum": "OK", "tur": tur, "sure": round(time.time() - bas, 1)}
    except YayinHatasi as e:
        durum = "ENGEL" if (e.http_kodu in GEO_CODES or kanal["url"] in YURTDISI_ENGELLI) else "HATA"
        return {**kanal, "durum": durum, "hata": str(e), "sure": round(time.time() - bas, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("liste")
    ap.add_argument("--rapor", default="rapor.md")
    ap.add_argument("--json", default="sonuc.json")
    ap.add_argument("--tur", type=int, default=3)
    ap.add_argument("--paralel", type=int, default=16)
    ap.add_argument("--baslik", default="TV Kanalları")
    ap.add_argument("--yurtdisi", default="", help="yurt dışından engelli bilinen linklerin listesi")
    ap.add_argument("--bekle", default="5", help="turlar arası bekleme (saniye, virgülle: 60,300)")
    args = ap.parse_args()
    if args.yurtdisi:
        YURTDISI_ENGELLI.update(yurtdisi_listesi_oku(args.yurtdisi))

    kanallar = m3u_oku(args.liste)
    sonuclar = {}
    bekleyen = kanallar
    bekleme = [int(x) for x in args.bekle.split(",") if x.strip()] or [5]
    for tur in range(1, args.tur + 1):
        paralel = args.paralel if tur == 1 else max(2, args.paralel // 4)
        TIMEOUT_SIMDI[0] = TIMEOUT if tur == 1 else TEKRAR_TIMEOUT
        with concurrent.futures.ThreadPoolExecutor(paralel) as havuz:
            for s in havuz.map(tek_test, bekleyen):
                sonuclar[s["url"]] = s
        bekleyen = [k for k in kanallar if sonuclar[k["url"]]["durum"] != "OK"]
        print(f"Tur {tur}: {len(bekleyen)} yayın başarısız", flush=True)
        if not bekleyen or tur == args.tur:
            break
        # Kısa süreli kesintiler raporlanmasın diye tekrar turları arasında zaman bırakılır.
        time.sleep(bekleme[min(tur - 1, len(bekleme) - 1)])

    hatalar = [sonuclar[k["url"]] for k in kanallar if sonuclar[k["url"]]["durum"] == "HATA"]
    engeller = [sonuclar[k["url"]] for k in kanallar if sonuclar[k["url"]]["durum"] == "ENGEL"]
    calisan = len(kanallar) - len(hatalar) - len(engeller)

    satirlar = [f"# {args.baslik} — günlük kontrol raporu", "",
                f"- Toplam yayın: **{len(kanallar)}**",
                f"- Çalışan: **{calisan}**",
                f"- Çalışmayan: **{len(hatalar)}**",
                f"- Yurt dışına kapalı olabilir (403/451): **{len(engeller)}**", ""]
    if hatalar:
        satirlar += ["## Çalışmayan yayınlar", "", "| Kanal | Grup | Hata | Link |", "|---|---|---|---|"]
        satirlar += [f"| {h['ad']} | {h['grup']} | {h['hata']} | {h['url']} |" for h in hatalar]
        satirlar.append("")
    if engeller:
        satirlar += ["## Yurt dışından erişim engeli olabilecekler",
                     "", "GitHub'ın test sunucuları ABD'de. Bu yayınlar Türkiye'de çalışıyor olabilir; "
                     "sadece bilgi içindir, uyarı sayılmaz.", "",
                     "| Kanal | Grup | Hata |", "|---|---|---|"]
        satirlar += [f"| {h['ad']} | {h['grup']} | {h['hata']} |" for h in engeller]
        satirlar.append("")
    satirlar.append(f"_Her başarısız yayın {args.tur} kez denendi._")

    with open(args.rapor, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar) + "\n")
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump({"toplam": len(kanallar), "calisan": calisan,
                   "hatalar": hatalar, "engeller": engeller}, f, ensure_ascii=False, indent=1)

    print(f"Toplam {len(kanallar)}, çalışan {calisan}, çalışmayan {len(hatalar)}, engelli {len(engeller)}")
    return 1 if hatalar else 0


if __name__ == "__main__":
    sys.exit(main())
