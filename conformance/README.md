# Uyumluluk (conformance) paketi

Motor hangi dilde yazılırsa yazılsın, simcfg derlenmiş formatını (`simcfg/compiled/v1`) tükettiğini iddia eden
her implementasyon bu dizindeki testleri geçmelidir. Referans implementasyon: `tools/simcfg/runtime/`.

| Dosya | Ne doğrular |
|---|---|
| `../schemas/v1/compiled-scenario.schema.json` | Derlenmiş senaryonun yapısı (motorun girdi sözleşmesi) |
| `expressions.json` | İfade dili semantiği: null davranışı, doğruluk, kısa devre, tip dışı karşılaştırmalar |
| `traces/*.trace.json` | Kural motoru semantiği: olay dağıtımı, rol çevirisi, öncelik, cooldown, kanal hakemliği |

## İz (trace) karşılaştırması

`traces/strait_patrol_engagement.trace.json`, `examples/scripts/strait_patrol_engagement.json` betiğinin
referans çalışma zamanındaki çıktısıdır:

```bash
bin/simcfg run examples/scripts/strait_patrol_engagement.json --golden conformance/traces/strait_patrol_engagement.trace.json
```

Motor implementasyonu aynı betiği çalıştırıp `rule.fire`, `rule.blocked`, `channel.preempted` ve `action`
kayıtlarını **aynı sırayla** üretmelidir. `command` kayıtlarının `stateChange` alanları referans etkileridir.
Gerçek motor bunları kendi eklentileriyle üretir; bu alanlar karşılaştırmaya dahil edilmeyebilir.

Bağlayıcı kurallar (R1-R9) `tools/simcfg/runtime/engine.py` dosyasının başında listelenmiştir.

Bir semantik bilerek değiştirildiğinde (ör. yeni bir kural ya da hakemlik değişikliği), izi yeniden üretip
farkı gözden geçirin ve commit edin.
