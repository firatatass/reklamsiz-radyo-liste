#!/usr/bin/env python3
"""Listeden kaldırılan yayınları saklar, her gün yeniden dener ve çalışmaya başlayanı geri ekler.

kaldirilanlar.m3u, kaldırılan her yayını kalıcı kimliğiyle (tvg-id) tutar:
  #EXTINF:-1 tvg-id="r-ornek" group-title="RADYO" liste="radyo" kaldirilma="2026-10-26" calisti="",Örnek FM
  https://ornek/stream
Bir yayın 2 gün üst üste çalışırsa (ABD'den ya da Türkiye'den) aynı kimlikle kendi listesine geri
eklenir ve o listenin sürümü artırılır. Tek günlük düzelmeler yüzünden liste gidip gelmesin diye
ilk çalıştığı gün sadece "calisti" tarihi yazılır.

Kullanım (deponun kökünde):
  python .github/kontrol/geri_ekle.py dene
  python .github/kontrol/geri_ekle.py kaldir "Ad 1|Ad 2" --liste radyo
Çıkış kodu: dene -> 0 değişiklik yok, 3 dosyalar değişti.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kontrol import USER_AGENT, YayinHatasi, uygulama_gibi_duzelt, yayin_test  # noqa: E402
from tr_kontrol import turkiye_dene  # noqa: E402

LISTELER = {"radyo": "radyo.m3u", "tv": "tv.m3u"}
KALDIRILANLAR = "kaldirilanlar.m3u"
SURUM = "surum.json"


def bugun():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).date().isoformat()


def oku(yol):
    """(satır sonu, başlık satırları, [[extinf, url], ...]) döner."""
    if not os.path.exists(yol):
        return "\r\n", ["#EXTM3U"], []
    metin = open(yol, "rb").read().decode("utf-8")
    nl = "\r\n" if "\r\n" in metin else "\n"
    satirlar = metin.split(nl)
    bas, kayitlar, i = [], [], 0
    while i < len(satirlar):
        s = satirlar[i]
        if s.startswith("#EXTINF"):
            kayitlar.append([s, satirlar[i + 1] if i + 1 < len(satirlar) else ""])
            i += 2
            continue
        if s.strip() and not kayitlar:
            bas.append(s)
        i += 1
    return nl, bas, kayitlar


def yaz(yol, nl, bas, kayitlar, beklenen):
    """Dosyayı önce bellekte hazırlar, kayıt sayısını doğrular, sonra yazar."""
    satirlar = list(bas)
    for e, u in kayitlar:
        satirlar += [e, u]
    veri = (nl.join(satirlar) + nl).encode("utf-8")
    sayi = sum(1 for s in satirlar if s.startswith("#EXTINF"))
    if sayi != beklenen:
        raise SystemExit(f"{yol}: beklenen {beklenen} kayıt, hazırlanan {sayi} — yazılmadı")
    with open(yol, "wb") as f:
        f.write(veri)


def nitelik(extinf, ad):
    m = re.search(rf'{ad}="([^"]*)"', extinf)
    return m.group(1) if m else ""


def nitelik_yaz(extinf, ad, deger):
    if re.search(rf'{ad}="[^"]*"', extinf):
        return re.sub(rf'{ad}="[^"]*"', f'{ad}="{deger}"', extinf)
    on, _, isim = extinf.rpartition(",")
    return f'{on} {ad}="{deger}",{isim}'


def temizle(extinf):
    """kaldirilanlar.m3u'ya özel nitelikleri çıkarır (listeye geri eklenecek hali)."""
    return re.sub(r'\s(liste|kaldirilma|calisti)="[^"]*"', "", extinf)


def isim(extinf):
    return extinf.rpartition(",")[2].strip()


def sirala_anahtar(ad):
    tablo = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    return ad.translate(tablo).casefold()


def ekle(kayitlar, yeni, liste):
    """Radyoda ada göre alfabetik yere, TV'de aynı grubun sonuna ekler."""
    if liste == "tv":
        grup = nitelik(yeni[0], "group-title")
        son = max((i for i, (e, _) in enumerate(kayitlar) if nitelik(e, "group-title") == grup), default=len(kayitlar) - 1)
        kayitlar.insert(son + 1, yeni)
        return
    anahtar = sirala_anahtar(isim(yeni[0]))
    for i, (e, _) in enumerate(kayitlar):
        if sirala_anahtar(isim(e)) > anahtar:
            kayitlar.insert(i, yeni)
            return
    kayitlar.append(yeni)


GENEL_KELIMELER = {"radyo", "radio", "fm", "am", "tv", "the", "canli", "online", "yayin", "turk", "turkiye"}


def kelimeler(metin):
    metin = metin.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).casefold()
    return {k for k in re.findall(r"[a-z0-9]+", metin) if len(k) > 1 and k not in GENEL_KELIMELER}


def yayin_adi(url):
    """Radyo akışının kendini tanıttığı ad (icy-name); yoksa boş döner."""
    if re.search(r"\.(m3u8|mpd)", url, re.I):
        return ""
    try:
        istek = urllib.request.Request(uygulama_gibi_duzelt(url), headers={"User-Agent": USER_AGENT, "Icy-MetaData": "1"})
        with urllib.request.urlopen(istek, timeout=15) as y:
            return (y.headers.get("icy-name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def ayni_radyo_mu(ad, url):
    """Eski link başka bir radyoya verilmiş olabilir: yayının adı radyonun adıyla hiç örtüşmüyorsa geri ekleme."""
    icy = yayin_adi(url)
    if not icy or not kelimeler(ad) or not kelimeler(icy):
        return True, icy
    sikisik = "".join(re.findall(r"[a-z0-9]+", icy.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).casefold()))
    ad_sikisik = "".join(re.findall(r"[a-z0-9]+", ad.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).casefold()))
    ortak = any(k in sikisik for k in kelimeler(ad)) or any(k in ad_sikisik for k in kelimeler(icy))
    return ortak, icy


def calisiyor_mu(url):
    try:
        yayin_test(url)
        return True, "çalışıyor"
    except YayinHatasi as e:
        try:
            durum, aciklama = turkiye_dene(url, str(e))
        except Exception as e2:  # noqa: BLE001
            return False, f"ABD: {e}; Türkiye testi yapılamadı ({type(e2).__name__})"
        return durum == "calisiyor", f"ABD: {e}; {aciklama}"


def dene():
    gun = bugun()
    nl, bas, bekleyen = oku(KALDIRILANLAR)
    if not bekleyen:
        print("Kaldırılan yayın yok.")
        return 0
    geri = {k: [] for k in LISTELER}
    kalan = []
    with concurrent.futures.ThreadPoolExecutor(8) as havuz:
        sonuclar = list(havuz.map(lambda k: calisiyor_mu(k[1]), bekleyen))
    for (e, u), (tamam, aciklama) in zip(bekleyen, sonuclar):
        print(f"{isim(e)}: {aciklama}", flush=True)
        if not tamam:
            kalan.append([nitelik_yaz(e, "calisti", ""), u])
        elif nitelik(e, "calisti") and nitelik(e, "calisti") != gun:
            ayni, icy = ayni_radyo_mu(isim(e), u)
            if ayni:
                geri[nitelik(e, "liste") or "radyo"].append([temizle(e), u])
            else:
                print(f"GERİ EKLENMEDİ: {isim(e)} linki artık '{icy}' adlı başka bir yayını çalıyor", flush=True)
                kalan.append([nitelik_yaz(e, "calisti", ""), u])
        else:
            kalan.append([nitelik_yaz(e, "calisti", gun), u])

    surum = json.load(open(SURUM, encoding="utf-8"))
    for liste, yeniler in geri.items():
        if not yeniler:
            continue
        lnl, lbas, kayitlar = oku(LISTELER[liste])
        once = len(kayitlar)
        mevcut = {nitelik(e, "tvg-id") for e, _ in kayitlar}
        yeniler = [y for y in yeniler if nitelik(y[0], "tvg-id") not in mevcut]
        for y in yeniler:
            ekle(kayitlar, y, liste)
            print(f"GERİ EKLENDİ ({liste}): {isim(y[0])}")
        if yeniler:
            yaz(LISTELER[liste], lnl, lbas, kayitlar, once + len(yeniler))
            surum[liste] += 1
    if any(geri.values()):
        with open(SURUM, "w", encoding="utf-8", newline="\n") as f:
            json.dump(surum, f, indent=2)
            f.write("\n")
    yaz(KALDIRILANLAR, nl, bas, kalan, len(kalan))
    return 3


def kaldir(adlar, liste):
    gun = bugun()
    istenen = {a.strip() for a in adlar.split("|") if a.strip()}
    lnl, lbas, kayitlar = oku(LISTELER[liste])
    knl, kbas, bekleyen = oku(KALDIRILANLAR)
    once_l, once_k = len(kayitlar), len(bekleyen)
    kalan, cikan = [], []
    for e, u in kayitlar:
        (cikan if isim(e) in istenen else kalan).append([e, u])
    bulunmayan = istenen - {isim(e) for e, _ in cikan}
    if bulunmayan:
        raise SystemExit("Listede bulunamadı: " + ", ".join(sorted(bulunmayan)))
    for e, u in cikan:
        e2 = nitelik_yaz(nitelik_yaz(nitelik_yaz(e, "liste", liste), "kaldirilma", gun), "calisti", "")
        bekleyen.append([e2, u])
        print(f"Kaldırıldı ve saklandı: {isim(e)}")
    yaz(LISTELER[liste], lnl, lbas, kalan, once_l - len(cikan))
    yaz(KALDIRILANLAR, knl, kbas, bekleyen, once_k + len(cikan))
    surum = json.load(open(SURUM, encoding="utf-8"))
    surum[liste] += 1
    with open(SURUM, "w", encoding="utf-8", newline="\n") as f:
        json.dump(surum, f, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser()
    alt = ap.add_subparsers(dest="komut", required=True)
    alt.add_parser("dene")
    k = alt.add_parser("kaldir")
    k.add_argument("adlar")
    k.add_argument("--liste", choices=list(LISTELER), default="radyo")
    args = ap.parse_args()
    if args.komut == "dene":
        return dene()
    kaldir(args.adlar, args.liste)
    return 0


if __name__ == "__main__":
    sys.exit(main())
