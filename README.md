# Reklamsız Radyo — kanal listeleri

Reklamsız Radyo uygulaması radyo ve TV listesini buradan indirir.

- `radyo.m3u` — radyo istasyonları
- `tv.m3u` — TV kanalları (uygulamada ses olarak çalınır)
- `surum.json` — liste sürümleri. **Bir listeyi değiştirdiğinde ilgili sayıyı bir artır**; uygulamalar sadece sayı değişince listeyi yeniden indirir.

Her kaydın `tvg-id` değeri kalıcı kimliktir; **asla değiştirme**. Kullanıcının sildiği kanallar ve favoriler bu kimlikle takip edilir. Link değişirse sadece link satırını güncelle.
