#!/usr/bin/env python3
"""ABD'den (GitHub) çalışmayan yayınları Türkiye'den (İstanbul) yeniden dener ve günlük maili hazırlar.

Türkiye'den deneme için check-host.net'in İstanbul'daki iki test noktası (tr1, tr2) kullanılır.
  - Ses gelmemesi, 404, sertifika hatası gibi yerden bağımsız hatalar doğrudan bozuk sayılır.
  - Diğer hatalarda herhangi bir noktadan net HTTP 200 gelirse yayın Türkiye'de çalışıyor sayılır.
  - Elle doğrulanmış yurt dışı engelli yayınlar (kanallar/yurtdisi_engelli.txt) Türkiye'den net bir
    red gelmedikçe çalışıyor sayılır.
  - Servise ulaşılamazsa yayın "Türkiye'den doğrulanamadı" olarak çalışmayanlarda gösterilir.

Ayrıca durum/gecmis.json içinde her bozuk yayının ilk bozuk görüldüğü gün tutulur; mailde
"kaç gündür çalışmıyor" yazar.

Kullanım:
  python tr_kontrol.py sonuc_tv.json sonuc_radyo.json --govde govde.md --baslik-dosyasi baslik.txt
Çıkış kodu: 1 = gerçekten çalışmayan yayın var (mail atılmalı), 0 = yok.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kontrol import uygulama_gibi_duzelt  # noqa: E402

API = "https://check-host.net"
DUGUMLER = ["tr1.node.check-host.net", "tr2.node.check-host.net"]
GECMIS = "durum/gecmis.json"


def api(yol):
    """Servis yoğunlukta isteği reddedebiliyor (ör. 429); 5 ve 15 sn arayla tekrar denenir."""
    istek = urllib.request.Request(API + yol, headers={"Accept": "application/json",
                                                       "User-Agent": "reklamsiz-radyo-kontrol"})
    for bekle in (5, 15, None):
        try:
            with urllib.request.urlopen(istek, timeout=30) as y:
                return json.load(y)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            if bekle is None:
                raise
            time.sleep(bekle)


def kontrol_baslat(tur, hedef):
    dugum = "".join("&node=" + d for d in DUGUMLER)
    return api(f"/check-{tur}?host={urllib.parse.quote(hedef, safe='')}{dugum}")["request_id"]


def sonuc_bekle(istek_id, sure=45):
    """Tüm noktalar cevap verene kadar (en fazla `sure` sn) bekler. {dugum: [sonuc] veya None}"""
    bitis = time.time() + sure
    while True:
        r = api(f"/check-result/{istek_id}")
        if all(r.get(d) is not None for d in DUGUMLER) or time.time() > bitis:
            return r
        time.sleep(3)


# Yerden bağımsız hatalar: sunucu cevap veriyor ama yayın yok/bozuk, ya da telefon bu yayını açamaz.
# Bunlar Türkiye'den de aynıdır; Türkiye test noktası içeriği okumadığı için yanıltabilir.
KESIN_HATALAR = ("HTTP 404", "HTTP 410", "SSL", "CERTIFICATE", "certificate", "yeterli veri gelmedi",
                 "parça çok küçük", "geçersiz yayın listesi", "yayın listesinde parça yok", "geçersiz DASH")

# Türkiye'de çalıştığı elle doğrulanmış, yurt dışına kapalı yayınlar (gizli kontrol deposunda).
BILINEN_YURTDISI = "kanallar/yurtdisi_engelli.txt"


def bilinen_yurtdisi():
    try:
        with open(BILINEN_YURTDISI, encoding="utf-8") as f:
            return {s.strip() for s in f if s.strip() and not s.startswith("#")}
    except FileNotFoundError:
        return set()


def turkiye_dene(url, abd_hatasi=""):
    """('calisiyor' | 'bozuk' | 'dogrulanamadi', açıklama) döner.

    Türkiye test noktası sadece ilk HTTP cevabını görür, yayının içeriğini okumaz. Bu yüzden sadece net
    "200" cevabı çalışıyor sayılır. Hiç bitmeyen radyo akışları orada zaman aşımına düşer ve
    takılı kalan bir sunucudan ayırt edilemez; bunlar için elle doğrulanmış yurt dışı listesine bakılır.
    """
    for k in KESIN_HATALAR:
        if k in abd_hatasi:
            return "bozuk", f"yayın sorunu yerden bağımsız ({abd_hatasi[:60]})"
    duz = uygulama_gibi_duzelt(url)
    try:
        r = sonuc_bekle(kontrol_baslat("http", duz))
    except Exception as e:  # noqa: BLE001
        r, hata = {}, f"Türkiye testi yapılamadı ({type(e).__name__} {getattr(e, 'code', '')})".replace(" )", ")")
    else:
        hata = ""
    aciklamalar, kesin_red = [], False
    for d in DUGUMLER:
        sonuc = r.get(d)
        for s in (sonuc if isinstance(sonuc, list) else []):
            # s = [başarılı mı, süre, mesaj, http kodu, ip]
            if not isinstance(s, list) or len(s) < 4:
                continue  # servis bazen hata nesnesi döndürüyor
            if s[0] == 1 and str(s[3]) == "200":
                return "calisiyor", "Türkiye'den HTTP 200"
            aciklamalar.append(f"{s[2]}" + (f" ({s[3]})" if s[3] else ""))
            kod = int(s[3]) if str(s[3]).isdigit() else 0
            if kod >= 400 or "refused" in str(s[2]).lower() or "no such" in str(s[2]).lower():
                kesin_red = True  # sunucu net bir hata verdi ya da hiç yok
    if url in bilinen_yurtdisi() and not kesin_red:
        return "calisiyor", "Türkiye'de çalıştığı bilinen, yurt dışına kapalı yayın"
    if hata:
        return "dogrulanamadi", hata
    if not aciklamalar:
        return "dogrulanamadi", "Türkiye test noktaları cevap vermedi"
    return "bozuk", "Türkiye'den: " + aciklamalar[0]


def gecmis_oku():
    try:
        with open(GECMIS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sonuclar", nargs="+")
    ap.add_argument("--govde", default="govde.md")
    ap.add_argument("--baslik-dosyasi", default="baslik.txt")
    args = ap.parse_args()

    bugun = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).date()
    gecmis = gecmis_oku()
    yeni_gecmis = {}
    gercek, sadece_yurtdisi, dogrulanamayan = [], [], []
    sayilar = {}

    for yol in args.sonuclar:
        with open(yol, encoding="utf-8") as f:
            veri = json.load(f)
        tur_adi = "TV" if "tv" in os.path.basename(yol).lower() else "radyo"
        sayilar.setdefault(tur_adi, 0)
        denenecek = veri.get("hatalar", []) + veri.get("engeller", [])
        with concurrent.futures.ThreadPoolExecutor(2) as havuz:
            tr_sonuclari = list(havuz.map(lambda h: turkiye_dene(h["url"], h.get("hata", "")), denenecek))
        for h, (durum, aciklama) in zip(denenecek, tr_sonuclari):
            print(f"{h['ad']}: {durum} - {aciklama}", flush=True)
            kayit = {**h, "tur_adi": tur_adi, "tr": aciklama}
            if durum == "calisiyor":
                sadece_yurtdisi.append(kayit)
                continue
            if durum == "dogrulanamadi" and any(k in h.get("hata", "") for k in ("HTTP 401", "HTTP 403", "HTTP 451")):
                # Türkiye testi yapılamadı ve ABD'den gelen hata yurt dışı engeli belirtisi: mail atılmaz, bilgi olarak yazılır.
                dogrulanamayan.append(kayit)
                continue
            ilk = gecmis.get(h["url"], bugun.isoformat())
            yeni_gecmis[h["url"]] = ilk
            kayit["gun"] = (bugun - datetime.date.fromisoformat(ilk)).days + 1
            gercek.append(kayit)
            sayilar[tur_adi] += 1

    os.makedirs(os.path.dirname(GECMIS), exist_ok=True)
    with open(GECMIS, "w", encoding="utf-8") as f:
        json.dump(yeni_gecmis, f, ensure_ascii=False, indent=1, sort_keys=True)

    satirlar = ["Bugünkü kontrolde hem ABD'den hem Türkiye'den (İstanbul) denenip çalışmayan yayınlar:", ""]
    if gercek:
        satirlar += ["| Yayın | Tür | Grup | Kaç gündür | ABD'den hata | Türkiye'den | Link |",
                     "|---|---|---|---|---|---|---|"]
        for g in sorted(gercek, key=lambda x: (-x["gun"], x["tur_adi"], x["ad"])):
            gun = "bugün ilk kez" if g["gun"] == 1 else f"{g['gun']} gündür"
            satirlar.append(f"| {g['ad']} | {g['tur_adi']} | {g['grup']} | {gun} | {g['hata']} | {g['tr']} | {g['url']} |")
        satirlar.append("")
    if sadece_yurtdisi:
        satirlar += ["Sadece yurt dışından açılmayan, Türkiye'de çalışan yayınlar (sorun sayılmadı, bilgi için):", ""]
        satirlar += [f"- {s['ad']} ({s['tur_adi']})" for s in sadece_yurtdisi]
        satirlar.append("")
    if dogrulanamayan:
        satirlar += ["Yurt dışından açılmayan ama bugün Türkiye'den doğrulanamayan yayınlar (Türkiye test servisi cevap vermedi; "
                     "büyük ihtimalle yurt dışı engeli, mail sebebi sayılmadı):", ""]
        satirlar += [f"- {s['ad']} ({s['tur_adi']}): {s['hata']}" for s in dogrulanamayan]
        satirlar.append("")
    satirlar.append("_ABD'de başarısız olan her yayın 1 ve 5 dakika arayla tekrar denendi, sonra Türkiye'den doğrulandı._")
    with open(args.govde, "w", encoding="utf-8") as f:
        f.write("\n".join(satirlar) + "\n")

    tarih = bugun.strftime("%d.%m.%Y")
    with open(args.baslik_dosyasi, "w", encoding="utf-8") as f:
        f.write(f"📺📻 {tarih}: {sayilar.get('TV', 0)} TV kanalı, {sayilar.get('radyo', 0)} radyo çalışmıyor")

    print(f"Gerçekten çalışmayan: {len(gercek)}, sadece yurt dışında açılmayan: {len(sadece_yurtdisi)}")
    return 1 if gercek else 0


if __name__ == "__main__":
    sys.exit(main())
