# Instagram Boş Nick Tarayıcı

`instagram.com/<user>/` üzerinde 404 kontrolü **güvenilmez**: deaktive/silinmiş
hesaplar 404 dönse bile o kullanıcı adı hâlâ alınamaz olabilir; ayrıca
Instagram'ın yasaklı/rezerve listesindeki nickler de görünmez ama sen
onları kayıt edemezsin.

Bu araç Instagram'ın resmi kayıt akışındaki `web_create_ajax/attempt/`
endpoint'ini kullanarak nickleri **dört net duruma** ayırır:

| Durum         | Anlamı                                                                  |
| ------------- | ----------------------------------------------------------------------- |
| `available`   | Boş, alınabilir.                                                        |
| `taken`       | Birinin elinde (profil deaktive / gizli olsa bile alınamaz).            |
| `reserved`    | Instagram tarafından rezerve edilmiş (`instagram`, `facebook`, vb.).    |
| `blocked_term`| Yasaklı bir substring içeriyor (`admin`, `abuse`, vb.).                 |

## Kurulum

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Tek nick kontrolü

```bash
ig-check cristiano zk7q xkcdtest9z9z9z instagram admin
```

Çıktı (JSON, satır satır):

```json
{"username": "cristiano", "status": "taken", "code": "username_is_taken", ...}
{"username": "zk7q", "status": "taken", "code": "username_is_taken", ...}
{"username": "xkcdtest9z9z9z", "status": "available", ...}
{"username": "instagram", "status": "reserved", "code": "username_invalid", ...}
{"username": "admin", "status": "blocked_term", "code": "username_invalid_substring", ...}
```

## Toplu tarama

### 4/5/6 haneli tüm alfanümerik kombinasyonlar

```bash
ig-scanner --length 4 5 6 --alphabet alnum --out results/
```

### Sadece harf

```bash
ig-scanner --length 4 --alphabet alpha --out results/
```

### Kendi listenden

```bash
ig-scanner --wordlist my_wordlist.txt --out results/
```

### Proxy havuzu ile (önerilir)

```bash
ig-scanner --length 4 \
    --proxies http://user:pass@proxy1:8080 http://user:pass@proxy2:8080 \
    --per-request-delay 3 \
    --out results/
```

Daha çok proxy varsa dosya kullan (örn. webshare'den indirdiğin liste):

```bash
cp examples/proxies.example.txt proxies.txt
# proxies.txt'i kendi proxy'lerinle doldur (her satıra bir tane)
ig-scanner --length 4 5 6 --proxies-file proxies.txt --out results/
```

`proxies.txt` dosyası `.gitignore` içinde olduğu için commit edilmez.

Her proxy bir worker olur, paralel çalışır. Tek IP'den 10–20 istekten sonra
Instagram seni `feedback_required` (HTTP 429) ile bloklar; ciddi bir tarama
için **proxy havuzu zorunludur**.

## Çıktı dosyaları

`--out results/` dizininde:

```
results/
├── available.txt   # boş nickler (asıl aradığın!)
├── taken.txt
├── reserved.txt
├── blocked.txt
├── invalid.txt
├── unknown.txt
├── all.jsonl       # tüm kontroller, JSON Lines (durum + kod + mesaj)
└── state.json      # resume için işlenmiş nickler
```

## Resume (kaldığı yerden devam)

Aynı `--out` dizini ile yeniden çalıştırırsan, daha önce işlenmiş nickler
otomatik atlanır. Bu sayede CTRL+C ile kesip günler sonra devam
edebilirsin.

## Hız hesabı

- 4 haneli alnum: `36^4 = 1.679.616` kombinasyon.
- 5 haneli alnum: `60.466.176`.
- 6 haneli alnum: `2.176.782.336`.

Tek IP'den ~10–20 istek sonrası block yiyorsun. Saniyede 1 istek atan **50
proxy'lik bir havuzla** günde ~4.3M istek yapabilirsin → 4 haneli yarım
gün, 5 haneli ~2 hafta, 6 haneli pratik değil. Önce wordlist (4–6 hane,
sözlük tabanlı) ile başlamak çok daha verimlidir.

## Sorumluluk

Bu araç Instagram'ın kullanım koşullarına aykırı kullanılabilir. Yalnızca
kendi nick'in için, eğitim amaçlı veya araştırma amaçlı kullan. Geniş
çaplı taramalar hesap/IP banı ile sonuçlanabilir.
