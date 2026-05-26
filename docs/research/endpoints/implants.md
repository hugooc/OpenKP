# Implants endpoint

Source HAR: `docs/research/captures/problems-allergies-and-more.har`, 2026-04-23
(full response body preserved). Implemented as `list_implants` 2026-05-26.

## What this is

The MyChart "Implants" device list (page route `/mychartcn/app/implants`):
implanted and explanted medical devices — pacemakers, ICDs and leads, stents,
intraocular lenses, orthopedic hardware, and similar. A new data class for
OpenKP. Useful for MRI-safety questions, device recall lookups, and sharing
exact device specs (manufacturer / model / serial / UDI) with a non-KP
provider.

Single legacy `/mychartcn/api/` POST, same `LoadListData`-style family as
problems and allergies. No pagination — the full list comes in one call.

| Feature | Endpoint | Status |
| --- | --- | --- |
| Full implant list + per-device detail | `POST /mychartcn/api/implants/GetImplants` body `{}` | ✅ Mapped + shipped. Real body. |

## Auth / anti-forgery

Standard `/mychartcn/` CSRF contract (see `problems.md`). Fetch one
`__RequestVerificationToken`, send it as a header. Referer is
`https://healthy.kaiserpermanente.org/mychartcn/app/implants`. Request body is
the empty object `{}`. No query params.

## Response shape

Two parallel structures:

- **`implantGroupList`** — a body-area ordering index: `[{area, implantIDs[]}]`.
  The literal area `"zzz"` is Epic's sentinel that sorts unknown-area devices
  last. We use this list only for ordering.
- **`implantList`** — the authoritative per-device detail, a dict keyed by
  device id. Every field below comes from here.
- `communityActive` — boolean, ignored.

```json
{
  "implantGroupList": [
    {"area": "Chest", "implantIDs": ["<id>"]},
    {"area": "Eye",   "implantIDs": ["<id>", "<id>"]},
    {"area": "zzz",   "implantIDs": ["<id>", ...]}
  ],
  "implantList": {
    "<id>": {
      "id": "WP-24...",                     // opaque Epic handle
      "name": "Fake Pacemaker Model Z",      // fabricated for this doc
      "type": "Pacemaker",                   // "Cardiac Implant", "Ophthalmology", ...
      "area": "Chest",                        // "" when unknown (→ null)
      "laterality": "Left",                   // "Right", "" (→ null)
      "status": "Implanted",
      "isExplant": false,
      "isExternal": false,
      "manufacturer": "ACME CARDIAC",
      "model": "FAKE-PACE",
      "serial": "SN-CHEST-9",
      "udi": "(01)...(17)...(21)...",         // full barcode; often "" for older devices
      "sdi": "00000000000000",                // GTIN portion of the UDI
      "lot": "",
      "comments": [],                          // empty in all observed data
      "description": [],                        // empty in all observed data
      "organizationLinks": [],                  // ignored
      "implantProcedure": {
        "isoDate": "January 3, 2024",          // MISNOMER: a display string, not ISO
        "deviceCount": "1",                     // string, or ""
        "provider": "DR FAKE",
        "facility": "Fake Surgery Center"
      },
      "explantProcedure": {"isoDate": "", "deviceCount": "", "provider": "", "facility": ""}
    }
  },
  "communityActive": false
}
```

(All device names, models, serials, and IDs above are fabricated/elided — the
real HAR contains the member's actual implanted devices, which are PHI.)

## Field mapping (scraper → `Implant`)

| Model field | Source key | Notes |
| --- | --- | --- |
| `id` | `id` (fallback: map key) | Required — device dropped if neither present. |
| `name` | `name` | |
| `type` | `type` | "Pacemaker", "Cardiac Implant", "Ophthalmology", ... |
| `area` | `area` | `""` → null. The `"zzz"` group sentinel never reaches here. |
| `laterality` | `laterality` | "Left" / "Right" / null. |
| `status` | `status` | "Implanted", "Explanted". |
| `is_explant` | `isExplant` | |
| `is_external` | `isExternal` | True for non-KP-sourced records. |
| `manufacturer` / `model` / `serial` | same | |
| `udi` | `udi` | Full UDI barcode. Empty for older devices (pre-UDI era). |
| `sdi` | `sdi` | Device-identifier portion of the UDI. |
| `lot` | `lot` | |
| `comments` / `description` | same | Lists; empty in all observed data, string shape assumed. |
| `implanted` | `implantProcedure` | → `ImplantProcedure`, null if all-empty. |
| `explanted` | `explantProcedure` | → `ImplantProcedure`, null if all-empty. |

`ImplantProcedure`: `date` (the misnamed `isoDate` display string), `date_iso`
(derived `"YYYY-MM-DD"` via `%B %d, %Y`, null if unparseable), `provider`,
`facility`, `device_count`.

## Behavior notes

- **`isoDate` is not ISO.** Kaiser sends `"January 3, 2024"`. We pass it
  through as `date` and additionally derive `date_iso`. Same misnomer trap as
  the visit-notes AVS date.
- **Kaiser always sends both procedure blocks**, even for a device that was
  never explanted (every field an empty string). We collapse an all-empty
  block to `None` so `explanted` is null unless an explant really happened.
- **Older devices have no UDI.** Pre-UDI-mandate implants come back with empty
  `udi`/`sdi` but populated `manufacturer`/`model`/`serial`. Don't treat a
  missing UDI as a parse failure.
- **A device can appear twice.** Live-verified 2026-05-26: the newest device
  came back as both a curated record (type "Cardiac Implant", with `area`,
  `udi`, and ordering `provider`) and a raw device-feed record (type
  "Pacemaker", name prefixed with a feed code like `Bsci_7677...`, no `udi` or
  `area`), sharing the same `serial` and implant date. Older devices had only
  the raw record — the structured/UDI record is a newer Epic feature. OpenKP
  returns **both rows faithfully and does not dedupe** (a thin substrate
  shouldn't hide data). A caller that wants distinct physical devices can
  collapse on `(serial, implanted.date_iso)`.
- No pagination.

## Open questions / future work

- **`comments` / `description` shape when populated.** Always empty in recon.
  If they ever carry clinically relevant notes (e.g. "MRI conditional"),
  confirm whether elements are strings or objects and adjust `_str_list`.
- **`status` enum.** Only "Implanted" observed. Capture an explanted device to
  confirm the "Explanted" string and whether other states exist.
- **UDI parsing.** We surface the raw UDI barcode. A future helper could split
  it into GTIN / expiration / serial AIDC fields if a caller needs structured
  UDI data.
