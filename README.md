# simcfg: Genişletilebilir Simülasyon Konfigürasyon Altyapısı

VR-Forces benzeri bir simülasyon için **teknoloji yığınından bağımsız**, JSON tabanlı, veri güdümlü
(data-driven) bir konfigürasyon altyapısı. Platformlar, sistemler, aksiyonlar, kural tabanlı davranışlar, alanlar ve senaryolar
aynı doküman modeliyle tanımlanır, kalıtımla türetilir ve katmanlı override'larla değiştirilir.

Motor (C++/C#/Java/Unity/Unreal…) bu dosyaları doğrudan okumaz. `simcfg compile` ile üretilen **tek, düzleştirilmiş, doğrulanmış**
JSON'u tüketir. Böylece konfigürasyon dili motordan bağımsız olarak evrilebilir.

```
 ┌─────────────── Konfigürasyon katmanı (bu depo) ────────────────┐        ┌──── Motor ────┐
 │  packages/core   packages/demo   packages/<sizin>              │        │               │
 │  SystemType → SystemTemplate ┐                                 │compile │ implementation│
 │  PlatformType → PlatformTemplate ─→ Scenario ─→ simcfg ────────┼──────→ │ id'leri ile   │
 │  EventType / ConditionType / ActionType / Maneuver / Behavior  │ (JSON) │ eklentilere   │
 │  AreaType / Enumeration / UnitCatalog                          │        │ bağlanır      │
 └────────────────────────────────────────────────────────────────┘        └───────────────┘
```

---

## Hızlı başlangıç

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # jsonschema (opsiyonel ama önerilir)

bin/simcfg validate                       # tüm paketler: şema + semantik + senaryo derleme
bin/simcfg tree --kind PlatformTemplate   # kalıtım ağacı
bin/simcfg describe demo.platform.fighter_mk2_ew        # her değer + hangi katmandan geldiği
bin/simcfg resolve core.system.iff.generic_combined --provenance
bin/simcfg compile demo.scenario.strait_patrol -o build/strait_patrol.compiled.json
bin/simcfg catalog -o build/catalog.json  # editörler için tip kataloğu + paletler
bin/simcfg derive core.platform.air.generic_fighter demo.platform.trainer --name "Trainer"
bin/simcfg run examples/scripts/strait_patrol_engagement.json   # referans kural motorunda olay betiği
bin/simcfg run examples/scripts/combined_arms_engagement.json   # müşterek harekât: kara/hava/İHA/piyade/sivil
bin/simcfg serve                          # editör: http://127.0.0.1:8765

.venv/bin/python -m unittest discover -s tests -v
```

`validate --json` tanıları makine-okunur verir (editör entegrasyonu); `--strict` uyarıları da hata sayar (CI için).

---

## Dizin yapısı

```
simcfg.workspace.json        çalışma alanı: şema dizini + paket listesi
schemas/v1/                  JSON Schema (draft 2020-12) sözleşmesi
  registry.json              kind -> şema dosyası + varsayılan klasör  (yeni tür = buraya kayıt)
  common.schema.json         ortak yapı taşları (descriptor, expression, rule, quantity...)
  document.schema.json       doküman zarfı
  <kind>.schema.json         tür başına şema
  compiled-scenario.schema.json   derlenmiş çıktı = MOTOR SÖZLEŞMESİ
packages/
  core/                      temel tipler + generic modeller  (simpkg.json manifestosu)
  demo/                      core'a bağımlı kullanıcı paketi: türetmeler, mixin, senaryolar
tools/simcfg/                referans araç zinciri (Python, bağımlılık: sadece jsonschema)
  runtime/                   referans kural motoru + ifade yorumlayıcısı (çalıştırılabilir şartname)
  server.py                  editör API'si (yerel, 127.0.0.1)
editor/                      editör prototipi (HTML + vanilla JS, derleme adımı yok)
conformance/                 motor uyumluluk paketi: ifade vektörleri + altın kural izleri
examples/scripts/            referans motorda çalıştırılan olay betikleri
tests/                       65 test: birleştirme, birim, kalıtım, derleme, hata yakalama, çalışma zamanı, API, birlikler
bin/simcfg                   CLI sarmalayıcısı
```

---

## 1. Doküman modeli

Her doküman aynı zarfa sahiptir (Kubernetes tarzı `kind` + `spec`):

```json
{
  "apiVersion": "simcfg/v1",
  "kind": "PlatformTemplate",
  "id": "demo.platform.fighter_mk2",
  "extends": "core.platform.air.generic_fighter",
  "abstract": false,
  "version": "0.1.0",
  "meta": { "name": { "tr": "Avcı Mk2", "en": "Fighter Mk2" }, "tags": ["fighter"] },
  "spec": { "...": "kind'a özgü içerik" }
}
```

| Alan | Anlam |
|---|---|
| `id` | Global kimlik `<paket>.<kategori>.<ad>`. Paket ad alanıyla başlamak **zorunlu**. |
| `extends` | Tek (`"a"`) veya çoklu/mixin (`["a","b"]`) kalıtım. Sadece aynı `kind`'dan türetilebilir. |
| `abstract` | Soyut dokümanlar örneklenemez, sadece türetilir (ör. `generic_fixed_wing`, `cm_dispenser`). |
| `deprecated` | `true` veya açıklama metni. Türetenlere uyarı verilir. |
| `x-*` | Her seviyede serbest uzantı alanı. Araç dokunmaz, motora/editöre özel meta için. |

Bir dosya tek doküman **veya doküman dizisi** içerebilir (ör. `events.json` içinde 16 olay).

### Doküman türleri (kinds)

| Katman | Kind | Ne tanımlar | Örnek |
|---|---|---|---|
| **Meta-model** (motor sözleşmesi) | `SystemType` | Sistem sınıfı: parametre şeması, gözlemlenebilir durum, komutlar, yaydığı olaylar, motor eklentisi | `core.systemtype.radar`, `rwr`, `iff`, `chaff_dispenser` |
| | `PlatformType` | Platform sınıfı + dinamik model; hangi sistem tipleri monte edilebilir | `core.platformtype.fixed_wing` |
| | `MunitionType` | Mühimmat sınıfı + uçuş/güdüm eklentisi | `core.munitiontype.guided_missile`, `ballistic` |
| | `EventType` | Kuralları tetikleyen olaylar + payload şeması | `core.event.rwr.lock` |
| | `ConditionType` | Kural editörü koşul paleti (native veya **veriyle/ifadeyle** tanımlı) | `core.cond.cm_available`, `core.cond.fuel_below` |
| | `ActionType` | Aksiyonlar: `native`, `command` (sistem komutuna eşleme), `composite` (veriyle) | `follow_route`, `dispense_chaff`, `maneuver` |
| | `Maneuver` | Segment dizisiyle veriyle tanımlı manevra | `break_turn`, `notch`, `split_s`, `jink` |
| | `AreaType` | Alan sınıfı + özellik şeması + görsel stil | `threat_zone`, `no_fly`, `cap_station` |
| | `Enumeration`, `UnitCatalog` | Yeniden kullanılabilir değer listeleri, birim dönüşümleri | `core.enum.radar_mode` |
| **Model** (kullanıcı verisi) | `SystemTemplate` | Değerleri doldurulmuş sistem modeli | `core.system.radar.generic_fcr` |
| | `Munition` | Değerleri doldurulmuş mühimmat (menzil, hız, Pk, karşı tedbir duyarlılığı) | `core.munition.generic_srm` |
| | `PlatformTemplate` | Generic/türetilmiş platform: parametreler, sistem yuvaları, davranış ekleri | `core.platform.air.generic_fighter` |
| | `Behavior` | Kural tabanlı davranış seti | `core.behavior.air.self_defense` |
| **Organizasyon** | `UnitTemplate` | Birlik: üyeler (platform × adet, rol, lider) + alt birlikler + formasyon | `core.unit.tank_platoon`, `infantry_squad` |
| **Senaryo** | `Scenario` | Taraflar, alanlar, rotalar, varlıklar, gruplar, senaryo kuralları, davranış override'ları | `demo.scenario.strait_patrol` |
| | `Package` | `simpkg.json`: sürüm, bağımlılıklar, motor eklenti gereksinimleri | `core`, `demo` |

> **Neden SystemType / SystemTemplate ayrımı?** `SystemType` "radar nedir, hangi ayarları ve durumları vardır" sorusunu cevaplar
> ve motordaki bir eklentiye karşılık gelir (`implementation`). `SystemTemplate` ise "bu belirli radarın değerleri"dir. Model editörü
> formları `SystemType`'tan üretir, kullanıcı `SystemTemplate` oluşturur/türetir. Yeni bir radar modeli eklemek kod gerektirmez;
> yeni bir *sistem sınıfı* (ör. lazer uyarı alıcısı) eklemek sadece bir `SystemType` dokümanı + motor eklentisi gerektirir.

---

## 2. Kalıtım ve birleştirme kuralları

Çözümleme (`resolve`) kalıtım zincirini **doğrusallaştırır** (soldan sağa, DFS post-order, ortak atalar bir kez) ve
her katmanın `spec`'ini sırayla uygular:

```
demo.platform.fighter_mk2_ew  extends [fighter_mk2, mixin.aft_dispensers]
  → generic_fixed_wing → generic_fighter → fighter_mk2 → mixin.aft_dispensers → fighter_mk2_ew
```

| Durum | Davranış |
|---|---|
| nesne + nesne | anahtar bazında derin birleştirme |
| sayı, metin, **dizi** | üzerine yazılır |
| `{"$delete": true}` | anahtarı kaldır (ör. bir sistemi veya kuralı sil) |
| `{"$replace": X}` | derin birleştirme yapmadan X koy |
| `{"$append": [..]}` / `{"$prepend": [..]}` | diziye ekle (`supportedModes: {"$append": ["4","5"]}`) |

**Tasarım ilkesi:** override edilmesi gereken koleksiyonlar *anahtarlı nesnedir*, dizi değildir (`systems`, `behaviors`,
`rules`, `entities`, `areas`). Böylece türetilmiş bir doküman tek bir sistemi/kuralı adıyla hedefleyebilir. Sıranın önemli olduğu
yerlerde (`waypoints`, `segments`, `actions`) dizi kullanılır.

`SystemType.emits` ve `PlatformType.emits` **kümülatiftir**: alt tip olay listesi üst tipinkine eklenir.

Her yaprak değerin hangi katmandan geldiği (`provenance`) izlenir. Editör bunu "miras alındı: generic_fighter" gibi göstermek
ve "üst değere dön" komutu için kullanır (`describe`, `resolve --provenance`, `catalog`).

---

## 3. Parametre tanımlayıcıları: model editörünün temeli

Tüm parametre, durum, komut argümanı, olay payload'ı ve alan özelliği **aynı tanımlayıcı (descriptor) diliyle** tanımlanır.
Model editörü formları, birimleri, aralıkları, açılır listeleri buradan üretir:

```json
"maxRange": {
  "type": "number", "unit": "m", "required": true, "min": 0,
  "label": { "tr": "Azami menzil", "en": "Max range" },
  "overridableAt": "instance",
  "ui": { "group": "Coverage", "order": 1, "widget": "slider", "advanced": false }
}
```

| Tip | Not |
|---|---|
| `number`, `integer` | `unit`, `min`, `max`, `step`. Değer: sayı (tanım biriminde), `"80 nmi"` veya `{"value":80,"unit":"nmi"}`. Derlemede tanım birimine çevrilir. |
| `boolean`, `string` (`pattern`), `enum` (`values` veya `enumRef`), `any` | |
| `ref` | Başka dokümana referans: `refKind`, `constraint.systemType` (alt tipler dahil) |
| `systemRole` | Platformdaki bir sistem yuvası/davranış rolü. `constraint.systemType` ile tip kısıtı |
| `entityRef`, `areaRef`, `routeRef`, `sideRef` | Senaryo nesneleri. Şablon seviyesinde zorunluluk kontrolü **senaryoya ertelenir** |
| `geoPoint`, `list` (`items`), `map` (`items`), `object` (`fields`), `table` (`axes`, ör. açıya göre RCS) | |
| `expression`, `action`, `actionList` | Kural/aksiyon içeriği |
| `args` | Argümanları **başka bir parametrenin hedefine göre** doğrulanır (`argsOf` + `argsFrom`). Ör. `maneuver.args` seçilen manevranın parametrelerine, `system_command.args` seçilen sistemin `commands.{command}.parameters` tanımına, `set_system_state.state` sistemin yazılabilir durumuna göre doğrulanır. |

Diğer özellikler: `default`, `defaultFrom` (`"parameters.capacity"`, ör. kalan chaff = kapasite), `nullable`, `readonly`
(motor yazar, aksiyonlar yazamaz), `overridableAt` (`template` < `instance` < `runtime`: değerin hangi seviyede değiştirilebileceği).

---

## 4. Platformlar, sistem yuvaları ve bulundurma durumu

```json
"systems": {
  "radar": { "template": "demo.system.radar.fcr_long_range", "placement": { "offset": { "x": 6 } } },
  "flare": { "parameters": { "capacity": 60 } },        // sadece şarjör kapasitesini değiştir
  "rwr":   { "fitted": false },                          // bu varyantta RWR yok
  "chaff": { "$delete": true }                           // yuvayı tamamen kaldır
}
```

* `fitted: false` olan sistem derlenmiş çıktıya girmez (`unfittedSystems` listesinde görünür). Türetilmiş bir dokümanda veya
  senaryo varlığında tekrar `true` yapılabilir.
* Çalışma anı açık/kapalı durumu ayrı bir kavramdır: `initialState.power`, `mode`, `transponderOn`… (her `SystemType`'ın `state`'i).
* `PlatformType.allowedSystemTypes` hangi sistem sınıflarının monte edilebileceğini kısıtlar.

---

## 5. Kural tabanlı davranışlar

Bir `Behavior` = parametreler + karatahta değişkenleri + gereken sistem **rolleri** + kurallar (Event–Condition–Action):

```json
"requires": { "systems": {
  "rwr":   { "systemType": "core.systemtype.rwr" },
  "chaff": { "systemType": "core.systemtype.chaff_dispenser", "optional": true }
}},
"rules": {
  "chaff_on_lock": {
    "priority": 80,
    "trigger":   { "event": "core.event.rwr.lock" },
    "condition": { "all": [ { "param": "reactToLock" },
                            { "cond": "core.cond.cm_available", "params": { "system": "chaff", "minCount": { "param": "chaffBurst" } } } ] },
    "actions":   [ { "action": "core.action.dispense_chaff", "params": { "system": "chaff", "count": { "param": "chaffBurst" } } } ],
    "cooldown":  "4 s"
  }
}
```

**Tetikleyiciler:** `{"event": id, "filter": expr}` · `{"every": "10 s"}` · `{"at": "15 min"}` · `{"edge": "rising|falling|both"}` (koşulun kenarı).

**İfade dili** (JSON AST; editörde blok/ağaç olarak gösterilebilir, motorda kolay yorumlanır):

| Biçim | Örnek |
|---|---|
| literal | `5`, `"track"`, `true` |
| değişken | `{"var": "self.systems.rwr.state.threatCount"}`, `self.fuel`, `self.params.maxG`, `event.bearing`, `bb.lastThreatBearing`, `entities.bandit_1.systems.radar.state.mode`, `scenario.time` |
| parametre | `{"param": "chaffBurst"}` |
| operatör | `{"op": "gt", "args": [a, b]}`: `eq ne lt le gt ge add sub mul div mod neg min max abs between in exists if` |
| mantık | `{"all": [...]}`, `{"any": [...]}`, `{"not": x}` |
| koşul tipi | `{"cond": "core.cond.in_area", "params": {"area": "red_mez"}}` |

Tüm değişken yolları **derleme anında** doğrulanır: rol var mı, sistem tipinin o durum alanı var mı, olayın o payload alanı var mı,
tipler uyumlu mu (yazım hatalarında öneri verilir: `threatCnt` → `threatCount`).

**Rol bağlama:** davranış platforma takılırken her rol bir mount'a bağlanır: açık `bindings` → aynı adlı mount → tipi uyan tek
mount. Opsiyonel bir rol bağlanamazsa, o rolü kullanan kurallar otomatik `enabled: false` olur (`disabledReason` ile).
Ör. chaff'sız ihraç varyantında `chaff_on_lock` kendiliğinden kapanır, flare kuralları çalışmaya devam eder.

**Hakemlik:** `ActionType.channels` (`movement`, `emissions`...) + kural `priority` + `arbitration.preemption`: yüksek öncelikli
kural aynı kanalı kullanan düşük öncelikli aksiyonu keser (ör. füze kaçınma manevrası rota takibini).

**Davranış türetme:** `demo.behavior.aggressive_self_defense` üst davranışın kurallarını adıyla değiştirir (`priority`),
siler (`$delete`) ve yenilerini ekler.

### Override katman sırası (sağdaki kazanır)

```
Behavior zinciri → platform eki (parameters/rules) → senaryo behaviorOverrides[<id> ve tüm ataları] → varlık overrides.behaviors.<ek>
```

Senaryo `behaviorOverrides["core.behavior.air.self_defense"]` o davranışı **ve türevlerini** kullanan tüm varlıklara uygulanır.

---

## 6. Aksiyonlar ve manevralar

| Uygulama türü | Ne zaman | Örnek |
|---|---|---|
| `native` | Motor eklentisi gerektiren temel aksiyon | `follow_route`, `maneuver`, `set_system_state` |
| `command` | Bir `SystemType` komutuna doğrudan eşleme, **kod gerektirmez** | `dispense_chaff` → `cm_dispenser.dispense`, `set_radar_mode` → `radar.setMode` |
| `composite` | Diğer aksiyonlardan veriyle oluşturulur | `demo.action.defensive_reaction` = sequence(chaff, jink, wait) |

Kontrol akışı da aksiyondur: `sequence`, `parallel`, `repeat`, `wait`. Aksiyon tipleri de türetilebilir: `dispense_chaff`,
`dispense_countermeasure`'dan sistem kısıtını ve varsayılanları değiştirerek türetilir.

Generic aksiyonlar sayesinde yeni sistem tipleri için yeni aksiyon yazmak gerekmez:
`core.action.set_system_state` (herhangi bir yazılabilir durum) ve `core.action.system_command` (herhangi bir komut).
Argümanları hedef sistemin tanımına göre doğrulanır.

### Silahlar ve angajman kuralları (ROE)

Silahlar ayrı bir kavram olarak değil, mevcut desenin uzantısı olarak modellenir:

* `MunitionType` / `Munition`: `SystemType` / `SystemTemplate` ile aynı tip/şablon deseni. Mühimmat; güdüm tipi, min/maks menzil,
  hız, Pk, `launchWarning` (atış RWR'da `missile_launch` olarak algılanır mı) ve `cmSusceptibility` (chaff/flare kandırma olasılığı) taşır.
* `core.systemtype.weapon_launcher`: fırlatıcı/silah istasyonu bir **sistemdir**. `loadout` istasyon → `{munition, count}` haritasıdır,
  bu yüzden türetmede tek istasyon değiştirilebilir: `"loadout": {"mrm": {"count": 6}}` veya `{"mrm": {"$delete": true}}`.
* Aksiyonlar `engage`, `set_master_arm`, `cease_fire` → fırlatıcı komutlarına `command` eşlemesi (kod yok).
* Koşullar `has_munition`, `target_in_weapon_range`, `target_identity`; olaylar `weapon.launched`, `weapon.impact`, `weapon.out_of_ammo`.
* `core.behavior.air.engage_hostiles` ve SAM davranışı **ROE** parametresi taşır (`hold` / `tight` / `free`, `overridableAt: runtime`).
  ROE de diğer parametreler gibi katmanlanır: gece varyantı senaryo genelinde `hold` der, `bandit_1` varlık seviyesinde `free` kalır.

Yeni tür eklemenin gerçek örneği budur: iki şema dosyası + `registry.json` satırı + `validate.py` içinde iki `@handler`.

### Kütüphane kapsamı (core)

| Alan | Platform tipleri | Generic platformlar |
|---|---|---|
| Hava (sabit kanat) | `fixed_wing`, `uav`, `loitering_munition` | avcı, taarruz uçağı, AEW&C, askeri nakliye, yolcu uçağı, iş jeti, hafif uçak (GA), MALE İHA, taktik İHA, dolanan mühimmat |
| Hava (döner kanat) | `rotary_wing`, `multirotor` | taarruz / genel maksat / sivil helikopter, quadcopter, ticari drone |
| Kara | `tracked_vehicle`, `wheeled_vehicle`, `lifeform`, `structure` | MBT, PMA (IFV), ZMA (APC), lojistik kamyonu, obüs, sivil araç/otobüs, piyade (er, manga lideri, makineli, tanksavar, MANPADS, ileri gözetleyici), sivil, silahlı düzensiz, bina, komuta yeri, köprü |
| Hava savunma | (kara tipleri) | orta / uzun menzilli SAM bataryası, SHORAD aracı (füze + top), çekili uçaksavar topu, erken ihbar radarı |
| Deniz | `surface_vessel` | karakol botu, fırkateyn, kargo gemisi |

**Sistem tipleri:** radar, RWR, IFF, EO/IR, görsel (göz/NVG/periskop), MAWS, LWR, lazer işaretleyici, karıştırıcı, telsiz, veri bağı
(Link 16 / İHA C2 / SATCOM / mesh), GNSS/INS, zırh, APS, chaff/flare/sis atar, fırlatıcı, taret/top, kargo/asker bölmesi.

**Mühimmat (23):** IR/ARH/SARH füzeler, ATGM (SACLOS/lazer), MANPADS, SHORAD, uzun menzilli SAM, lazer/GPS güdümlü bomba,
120 mm APFSDS/HEAT, 30/35 mm, 7.62/5.56 mm, RPG, 70 mm roket, 155 mm obüs, 81 mm havan, 76 mm deniz topu.
Her mühimmat `targetDomains` (hava/kara/su üstü), `warhead`, `penetration` taşır; angajman koşulları hedef ortamını kontrol eder.

**Aksiyonlar (~45):** rota/nokta/yörünge/hover/iniş/kalkış/takip/alan devriyesi/kaçış/üsse dönüş/arazi takibi/formasyon/terminal saldırı;
duruş, siper, araca binme; sensör yönlendirme, lazer işaretleme, karıştırma, mesaj; angajman, mevziye atış, baskı ateşi; chaff/flare/sis;
yükleme/indirme; kontrol akışı (sequence/parallel/repeat/wait/değişken/kural aç-kapa).

**Davranışlar (17):** öz savunma (RWR), MAWS savunması, angajman (ROE), SAM bataryası, SHORAD, araç muharebesi, araç öz koruması
(LWR→sis+zigzag), taarruz helikopteri (NOE + pop-up ATGM + lazer + yeniden maskelenme), İHA ISR yörüngesi, bağlantı kaybı prosedürü,
dolanan mühimmat (ara-vur), piyade (siper al, karşılık ver, temas kes), sivil (ateş/patlamada panik ve kaçış), sivil uçuş (7700, ATC yönlendirme),
temas raporlama, asker taşıma (LZ'de in-indir-kalk), rota devriyesi, yakıt yönetimi.

### Birlikler (UnitTemplate)

```json
"units": {
  "blue_tanks": { "template": "core.unit.tank_platoon", "side": "blue", "callsign": "TIGER",
                  "position": { "lat": 39.62, "lon": 27.20 }, "heading": 45,
                  "allMembers": { "behaviors": { "combat": { "parameters": { "roe": "tight" } } } },
                  "overrides":  { "blue_tanks_wing_2": { "systems": { "aps": { "fitted": false } } } } }
}
```

Derleyici birliği formasyon konumlarıyla varlıklara açar: `blue_tanks_lead` (TIGER 1), `blue_tanks_wing_1..3`. Alt birlikler özyinelemeli
açılır: mekanize takım = 3 PMA + 3 manga × (lider + 2 tim × 4) = 30 varlık. Formasyonlar: column, line, wedge, vee, echelon_left/right,
diamond, box, spread… Çıktıda `units` hiyerarşisi ve her varlıkta `unit.leaderEntity` bulunur; motor bunu formasyon tutmak için kullanır.
Yama sırası: platform şablonu ← birlik üyesi `overrides` ← örnek `allMembers` ← örnek `overrides[<varlık>]`.

**Kural aksiyonları sıralıdır:** bir kuralın `actions` listesi bir `sequence` gibi çalışır. Süreli bir aksiyon (ör. `land`) bitmeden
sonraki başlamaz. Bu yüzden "in → indir → bekle → kalk" tek kuralda yazılır; ayrı kurallara bölünürse ikincisi kanal hakemliğine takılır.

---

## 7. Senaryo

`spec`: `time`, `environment` (serbest), `sides` (+ ilişkiler), `areas`, `routes`, `entities`, `groups`, `variables`,
`rules` (senaryo seviyesi; aksiyonlar `"on": "<varlık>"` ile bir varlığa yönlendirilir), `behaviorOverrides`.

* **Alanlar:** `circle`, `polygon`, `corridor`, `sector`, `ellipse` + irtifa bandı + taraf ilişkisi + `AreaType` özellikleri.
* **Varlıklar:** `platform` + `side` + `initial` + `overrides` (PlatformTemplate.spec'e yama: parametreler, sistemler, davranışlar) + `plan`.
* **Senaryo varyantları:** senaryolar da `extends` ile türetilir. `strait_patrol_night` zamanı/havayı değiştirir, bir varlığın
  platformunu değiştirir, bir varlığı siler (`$delete`), yeni varlık ekler, kural tetik zamanını değiştirir ve bir kuralı siler.

---

## 8. Derlenmiş çıktı: motor sözleşmesi

`simcfg compile` → `apiVersion: simcfg/compiled/v1`:

* Tüm kalıtım çözülmüş; her nesnede `lineage` (soy zinciri) ve tipin `implementation` id'si var.
* Tüm sayılar tanımlayıcı birimine çevrilmiş (SI ağırlıklı: m, m/s, s, deg, Hz, W, kg…).
* Varsayılanlar ve `defaultFrom` uygulanmış; takılı olmayan sistemler çıkarılmış; roller mount'lara bağlanmış.
* Kurallar normalize edilmiş (`cooldown`, `every`, `at` saniye), devre dışı kurallar gerekçesiyle işaretlenmiş.
* `catalog`: kullanılan tüm aksiyon/koşul/olay/manevra/enum/tip tanımları gömülü. Motor çıktıyla birlikte başka bir dosyaya ihtiyaç duymaz.

* `munitions`: yüklü mühimmatların normalize edilmiş tam tanımları.

Sözleşme üç parçadan oluşur ve motor dilinden bağımsızdır:

| Parça | Dosya | Ne sağlar |
|---|---|---|
| Yapı | `schemas/v1/compiled-scenario.schema.json` | Motorun girdi formatı. `compile` her çıktıyı buna karşı kontrol eder. Motorlar bilinmeyen alanları yok sayar. |
| Semantik | `tools/simcfg/runtime/` (Python referans) | Olay dağıtımı, rol çevirisi, öncelik/cooldown, kanal hakemliği. Bağlayıcı kurallar R1–R9 `engine.py` başında. |
| Uyumluluk | `conformance/` | 44 ifade vektörü + 2 altın kural izi. Her motor implementasyonu bunları geçmeli. |

Motor tarafının yapması gerekenler:
1. Derlenmiş JSON'u yükleyen bir okuyucu.
2. `implementation` / `handler` id'lerini eklentilere eşleyen bir kayıt (registry).
3. İfade yorumlayıcısı (~100 satırlık kapalı AST; `runtime/expr.py` birebir taşınabilir).
4. Kural motoru (`runtime/engine.py` R1–R9).

Referans çalışma zamanı fizik içermez. Komut etkileri (sayaçlar, modlar, olay üretimi) sözleşmeyi göstermek için basit
tutulmuştur; ör. SAM'in `fire` komutu hedefin RWR'ına `missile_launch` üretir:

```
t=5.00  sam_1    ▶ air_defense.engage_in_mez  (öncelik 70)
t=5.00  sam_1        · engage(launcher=launcher, target=viper_1, salvo=2)
t=5.00  viper_1  ⚡ core.event.rwr.missile_launch  system=rwr, source=sam_1, bearing=130.9
t=5.00  viper_1  ▶ self_defense.flare_and_break_on_launch  (öncelik 100)
t=5.00  viper_1  ↯ self_defense.flare_and_break_on_launch 'movement' kanalını patrol.start_patrol'dan aldı
t=5.00  viper_1          ↳ flare.dispense  remaining: 60→58
```

---

## 9. Genişletme noktaları

| İhtiyaç | Yapılacak | Kod? |
|---|---|---|
| Yeni radar/uçak modeli | `derive` ile türet, değerleri değiştir | Hayır |
| Platform varyantı (sistem çıkar/ekle) | `fitted:false`, `$delete`, mixin | Hayır |
| Yeni davranış / kural değişikliği | `Behavior` türet; senaryoda `behaviorOverrides` | Hayır |
| Yeni manevra | `Maneuver` dokümanı (segment dizisi) | Hayır |
| Yeni bileşik aksiyon | `ActionType` `composite` | Hayır |
| Sistem komutuna aksiyon | `ActionType` `command` | Hayır |
| Veriyle tanımlı koşul | `ConditionType` `expression` | Hayır |
| Yeni alan sınıfı / enum / birim | `AreaType` / `Enumeration` / `UnitCatalog` | Hayır |
| Yeni sistem sınıfı (ör. MAWS, jammer, datalink) | `SystemType` + motor eklentisi | Sadece motorda |
| Yeni native aksiyon / koşul | `ActionType` / `ConditionType` `native` + motor handler'ı | Sadece motorda |
| Yeni platform sınıfı (gemi, helikopter) | `PlatformType` + dinamik model eklentisi | Sadece motorda |
| Tamamen yeni doküman türü (ör. `WeaponType`, `Formation`, `CommPlan`) | `schemas/v1/<x>.schema.json` + `registry.json` kaydı; opsiyonel semantik doğrulayıcı: `validate.py` içinde `@handler("X")` | Araçta küçük |
| Organizasyona özel meta | `x-*` alanları | Hayır |

Paketler (`simpkg.json`) bağımlılık ve sürüm aralığı (`^1.0.0`, `~1.2.0`, `>=`) beyan eder. Bir paket sadece bağımlı olduğu
paketlerden türetebilir. Kurum içi kütüphaneler (`acme`), proje paketleri ve senaryo paketleri ayrı yaşayabilir.

---

## 10. Editör

`bin/simcfg serve` → `http://127.0.0.1:8765`. Derleme adımı olmayan bir prototip (`editor/`) ve yerel API (`tools/simcfg/server.py`):

* **Model editörü** (Platform / Sistem / Mühimmat): formlar tip tanımlayıcılarından üretilir (gruplar, birimler, enum/ref açılır listeleri).
  Her satırda bu dokümandaki değer, efektif (normalize) değer ve kaynak (miras alınan doküman) görünür; ↺ override'ı kaldırır.
  Sistem yuvaları: şablon değiştir, takılı/sökülü, mount parametreleri, yuva ekle/kaldır. Davranış ekleri: parametre değerleri, platform bazında kural aç/kapat.
* **Davranış editörü**: kural kartları (öncelik sırasıyla), tetikleyici seçici (olay paleti + payload alanları), koşulun okunabilir hali
  (`($roe ≠ "hold" VE event.identity = "hostile")`), aksiyon listesi (palet + tanımlayıcıdan üretilen parametre alanları), cooldown/maxFires.
  Miras alınan kuralda yapılan değişiklik sadece değişen alanı override olarak yazar; silme `$delete` üretir.
* **Senaryo**: varlıklar (platform/taraf değiştir, varyantta sil/geri al), alanlar, davranış override'ları, senaryo kuralları (`on:` hedefli), **Derle** sekmesi.
* Her değişiklik kaydetmeden önce sunucuda **canlı doğrulanır** (taslak bellek içi yama olarak çalışma alanına uygulanır); hatalar ilgili satırda ve alt panelde görünür.
* Kaydetme, dizi dosyalarında sadece ilgili elemanı değiştirir. "Türet…" yeni bir türev dokümanı doğru paket/klasöre oluşturur. `Ctrl/⌘+S` kaydeder.

Sunucu sadece `127.0.0.1`'e bağlanır, kimlik doğrulaması yoktur. Tek kullanıcılı yerel geliştirme içindir, paylaşımlı sunucuda çalıştırmayın.

`bin/simcfg catalog` başka bir editörün (ör. motorun kendi aracı) ihtiyaç duyduğu her şeyi tek JSON'da verir:

* **Model editörü:** her `SystemType`/`PlatformType` için tanımlayıcılar (form, birim, aralık, grup, widget ipucu); her şablonun
  çözülmüş değerleri, `lineage`, `children` ve `provenance` (miras göstergesi). "Türet…" komutu = `derive`.
* **Davranış editörü:** `palettes.events` (kaynağa göre), `palettes.conditions` / `palettes.actions` (kategoriye göre),
  `maneuvers`, `expressionOperators`, `variableRoots`. Rol tanımları ve parametreler formdan düzenlenir.
* **Doğrulama:** editör kaydettiğinde `validate --json` çalıştırıp tanıları (`doc`, `path`, `code`, `message`) satır/alan bazında gösterir.
* Dosyalardaki `$schema` alanı sayesinde VS Code vb. düzenleyicilerde otomatik tamamlama ve anlık şema kontrolü çalışır.

---

## Bilinçli tasarım kararları

* **JSON Schema yapıyı, araç anlamı doğrular.** Şema tek başına "bu RWR rolü gerçekten var mı", "bu birim uyumlu mu",
  "bu kural hiç tetiklenir mi" sorularını cevaplayamaz; bunlar `simcfg`'nin semantik katmanında.
* **Şemalar çözülmüş (resolved) dokümanı tarif eder.** Türetilmiş ham dokümanlar kısmi olabilir ve direktif içerebilir;
  ham dosyalar sadece zarf şemasıyla, birleştirilmiş sonuç ise tür şemasıyla doğrulanır.
* **Referans araç Python'da**, ama sözleşme JSON Schema + derlenmiş çıktı formatıdır. Motor dili seçildiğinde araç yeniden yazılmak
  zorunda değil; istenirse aynı kurallar (birleştirme, doğrusallaştırma, birim) motor tarafında da uygulanabilir.
* **İfade dili kasıtlı olarak küçük ve kapalı.** Turing-tam betik yerine doğrulanabilir AST: editörde görselleştirilebilir,
  derleme anında tip/yol kontrolü yapılır, motorda güvenli yorumlanır. Karmaşık mantık `native` koşul/aksiyon olarak eklenir.

## Sonraki adımlar (öneri)

* Motor dili seçimi → `runtime/` semantiğinin o dile taşınması + `conformance/` paketinin CI'da koşturulması
* `Formation`, `CommPlan`, `Datalink` gibi ek türler; hasar/öldürme modeli için `weapon.impact` semantiğinin netleştirilmesi
* Editör: harita üzerinde alan/rota çizimi, koşullar için sürükle-bırak ifade ağacı, çok kullanıcılı kilitleme
* Şema sürüm geçişleri için `simcfg migrate` (apiVersion `simcfg/v2` geldiğinde)
