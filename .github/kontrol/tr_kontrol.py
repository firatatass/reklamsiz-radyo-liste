#!/usr/bin/env python3
"""ABD'den (GitHub) çalışmayan yayınları Türkiye'den (İstanbul) yeniden dener ve günlük maili hazırlar.

Türkiye'den deneme için check-host.net'in İstanbul'daki iki test noktası (tr1, tr2) kullanılır.
  - Herhangi bir noktadan HTTP 200 gelirse yayın Türkiye'de çalışıyor sayılır.
  - Radyo akışları hiç bitmediği için HTTP denemesi ~25 sn sonra "zaman aşımı" ile biter. Bu durumda
    sunucunun portuna TCP bağlantısı da kurulabiliyorsa yayın akıyor demektir (kapalı sunucu ya hemen
    reddeder ya da hiç bağlanmaz).
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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kontrol import uygulama_gibi_duzelt  # noqa: E402

API = "https://check-host.net"
DUGUMLER = ["tr1.node.check-host.net", "tr2.node.check-host.net"]
GECMIS = "durum/gecmis.json"


def api(yol):
    istek = urllib.request.Request(API + yol, headers={"Accept": "application/json",
                                                       "User-Agent": "reklamsiz-radyo-kontrol"})
    with urllib.request.urlopen(istek, timeout=30) as y:
        return json.load(y)


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


def turkiye_dene(url, abd_hatasi=""):
    """('calisiyor' | 'bozuk' | 'dogrulanamadi', açıklama) döner."""
    if abd_hatasi in ("HTTP 404", "HTTP 410"):
        # "Bulunamadı" yurt dışı engeli değildir; yayın sunucudan kalkmış.
        # (Türkiye test noktasının önbellekli bir CDN kopyasından 200 alması yanıltabiliyor.)
        return "bozuk", "yayın sunucuda yok (404), Türkiye testi gerekmedi"
    url = uygulama_gibi_duzelt(url)
    try:
        r = sonuc_bekle(kontrol_baslat("http", url))
    except Exception as e:  # noqa: BLE001
        return "dogrulanamadi", f"Türkiye testi yapılamadı ({type(e).__name__})"
    aciklamalar, zaman_asimi = [], False
    for d in DUGUMLER:
        for s in r.get(d) or []:
            # s = [başarılı mı, süre, mesaj, http kodu, ip]
            if not s:
                continue
            if s[0] == 1 and str(s[3]) == "200":
                return "calisiyor", "Türkiye'den HTTP 200"
            if str(s[3]) in ("301", "302", "303", "307", "308"):
                # Test noktası yönlendirmeyi takip etmiyor; sunucu ayakta ve cevap veriyor.
                return "calisiyor", "Türkiye'den yönlendirme (sunucu ayakta)"
            mesaj = f"{s[2]}" + (f" ({s[3]})" if s[3] else "")
            aciklamalar.append(mesaj)
            if "timed out" in str(s[2]).lower() and s[1] and float(s[1]) >= 20:
                zaman_asimi = True
    if not aciklamalar:
        return "dogrulanamadi", "Türkiye test noktaları cevap vermedi"
    if zaman_asimi and not url.lower().split("?")[0].endswith((".m3u8", ".mpd")):
        p = urllib.parse.urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        try:
            t = sonuc_bekle(kontrol_baslat("tcp", f"{p.hostname}:{port}"), sure=30)
            if any(isinstance(x, dict) and "time" in x for d in DUGUMLER for x in (t.get(d) or [])):
                return "calisiyor", "Türkiye'den akış devam ediyor"
        except Exception:  # noqa: BLE001
            pass
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
    gercek, sadece_yurtdisi = [], []
    sayilar = {}

    for yol in args.sonuclar:
        with open(yol, encoding="utf-8") as f:
            veri = json.load(f)
        tur_adi = "TV" if "tv" in os.path.basename(yol).lower() else "radyo"
        sayilar.setdefault(tur_adi, 0)
        denenecek = veri.get("hatalar", []) + veri.get("engeller", [])
        with concurrent.futures.ThreadPoolExecutor(4) as havuz:
            tr_sonuclari = list(havuz.map(lambda h: turkiye_dene(h["url"], h.get("hata", "")), denenecek))
        for h, (durum, aciklama) in zip(denenecek, tr_sonuclari):
            print(f"{h['ad']}: {durum} - {aciklama}", flush=True)
            kayit = {**h, "tur_adi": tur_adi, "tr": aciklama}
            if durum == "calisiyor":
                sadece_yurtdisi.append(kayit)
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
