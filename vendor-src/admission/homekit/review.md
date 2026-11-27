## Code review: APPROVED
Runtime verification: target — ARM ELF identity, HAP crypto exercises and 18 bridge tests on the donor. No production service was started during this test.
## Contracts: docs/contracts/dependency-release-policy.md — exact optional HomeKit candidate and factory donor scope only.

The exact wheel matches twelve affected OpenSSL version ranges. Nine of the twelve have a high external score. The production `SafeDriver` replaces the upstream server with `SafeHAPServer`. Its asyncio listener uses plain TCP at bridge.py lines 153–160 and 189, with HomeKit pairing and encryption inside HAP. The paho client uses its default TCP transport without TLS at mqtt_link.py lines 23–34 and 88–91. Multicast DNS uses DNS datagrams. These paths do not invoke QUIC or TLS DANE/Suite B.

HAP-python 5.0.0 imports the raw Ed25519 and X25519 constructors, persists pairing keys as JSON/raw bytes, derives keys with HKDF-SHA512 and instantiates ChaCha20-Poly1305 for encrypted frames. The relevant wheel source is pyhap/{encoder,hap_crypto,hap_handler,state}.py. The chacha wrapper aliases PyCA's typed ChaCha20Poly1305. PyCA's tagged ed25519.rs, x25519.rs and aead.rs select their typed OpenSSL implementations. No served characteristic takes PEM, CMS, CRL or a cipher choice from the peer. The application creates bridge/accessory instances for projected services. Alice's scene_devices can import Rules and its bridge_roster can load the Modbus producer. Family classification calls prepare_poller_registration and may construct SpodesPoller. This constructor saves the chosen DLMS settings and leaves its transport and session unset. The separate SPODES crypto implementation uses AES in GCM mode. The twelve prerequisites below are unreachable through these fixed interfaces.

The factory donor's recorded venv inventory is {"exists":false,"distributions":[]}. The temporary unprivileged ARM test corroborates capability of the identified candidate. Deployment of the optional service and validation of its actual source origins were outside that experiment. For a release decision scoped to absent donor 136 require explicit and fresh device/absence evidence for the interpreter and its active/effective unit, frozen optional lock, wheel and product/peer sources and provider records. Any installed or unknown profile stays under investigation. Inherited site packages, overrides, conditional imports and other consumers preclude carrying the assessment to an arbitrary installation.

The 445 ranges come from the registered OpenSSL archive. Each identified product has defaultStatus unaffected. 443 affected ranges have exclusive ends in historical OpenSSL 0.x/1.x release lines; their letter suffixes do not change the major release. One identifies the historical FIPS object module explicitly, and one marks the unrelated singleton version 1.1.0 explicitly unaffected. Each is outside the reviewed 4.0.3 candidate. Keep the original rows and severity. The live provider tree and immutable review inputs must still agree during replay. [Official release versioning](https://www.openssl-library.org/policies/general/versioning-policy/) and [upstream OpenSSL 4.0 advisories](https://openssl-library.org/news/vulnerabilities-4.0/).

### R3 reassessment on 28 November 2026

The first R3 scan, report `55cfd5e5368da34454ca500e16bf08d9b72b8bfddf12d5fddde5d5abd2e45f7d`, rejected the previous source hash. The independent comparison covered every member of the original product manifest at `1164445d3aee269339ed5d37702823fd7fcfbfa9` and `51d45e139feecce4a20583237cc028142146113a`. The 112 common entries have one changed file, Rules' `capability_binding.py`. The install profiles below bind the complete Alice installer, Rules installer and shared library as one triple per revision.

The HomeKit scene reader imports the Alice rules store. The store imports the Rules device index and its capability binding. When Rules needs the roster, its first choice is the copy in its own package. Both installer recipes copy the reviewed Alice roster into that package. The checkout fallback now imports `sa02m_alice.config.bridge_roster`. The Alice package initializers contain a version assignment and documentation. In separate isolated Python processes I loaded both layouts through the actual Rules function. They resolved to the two expected module names and the same reviewed file bytes. The changed fallback removes the previous addition of the Alice config directory to the Python search path. Transport initialization, HAP pairing, key decoding and cryptographic operations are unchanged.

The independently captured factory profile reported the HomeKit package and interpreter absent, its unit inactive and missing, and zero matching processes. I compared all twelve raw CVE response digests and the candidate ranges, severities and component bytes with the original review. The 445 preserved rows cover 236 CVEs and retain their original digest. Of these, 443 affected rows have an exclusive upper bound on release line 0.x or 1.x. The other two retain the historical FIPS lineage and explicitly unaffected singleton described below. The R3 scope therefore retains the twelve recorded dispositions only when the release checker independently confirms a fresh, matching absent-donor profile, the common graph, one complete installer triple, the registered ARM artifact and the provider records. Installed or unknown profiles remain under investigation.

```json
{
  "schema": 1,
  "assessment": "Independent SA-02m dev-reviewer, 2026-11-27",
  "scope": "Exact optional HomeKit candidate and independently observed absent factory donor 136 only",
  "artifact": {
    "elf": "cryptography/hazmat/bindings/_rust.abi3.so",
    "elf_sha256": "3547bf7902477933b7355dff3c88bddb46078624dca717921d48ffd4876d8d65",
    "pyca_workspace_crates": [
      {
        "name": "cryptography-cffi",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-crypto",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-keepalive",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-key-parsing",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-openssl",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-x509",
        "parent": "cryptography",
        "version": "0.50.2"
      },
      {
        "name": "cryptography-x509-verification",
        "parent": "cryptography",
        "version": "0.50.2"
      }
    ],
    "sboms": {
      "cryptography-50.0.2.dist-info/sboms/cryptography-rust.cyclonedx.json": "afd2189299bdca8a1a2359dc20d00684be6f1535c5bd57825a1d4e97330d1307",
      "cryptography-50.0.2.dist-info/sboms/sbom.json": "99ed88aec0191cbbff601763a0069f87234b4af97c79c345ff8ca41f4f5dfc27"
    },
    "version": "4.0.3",
    "wheel": "cryptography-50.0.2-cp311-abi3-manylinux_2_31_armv7l.whl",
    "wheel_sha256": "ac9ed99d81760c62fe89d5f0815cdfa1ba9a35141cf30f1c2d044f04b4803d2e"
  },
  "product_files_sha256": "151d4216b60621530372749d3356a5a0e836b747c6c27062f01809e8264ec235",
  "package_files_sha256": "7e6de1db32a22786ac25788f83d286db6ac50088f2c9a8f3d2973daea7a01cfc",
  "builder_inputs_sha256": "a37d21eff438470322e48d79ab5c10c22d873c367428628d3cbf2afd2f2f0867",
  "extra_source_files": {
    "etc/sa02m-homekit/sa02m-homekit.conf": "7ef6ec23074899765c6e9aed2266b87d1d8ad38f7b4782857efe926cd61ba5f2"
  },
  "upstream_archive_sha256": "de55b8338b01a3c5e4ad4e4f2a989e8a63121639285a17995ea4d45dd72b1453",
  "upstream_records": 309,
  "upstream_compared_semver_ranges": 401,
  "legacy_ranges": {
    "count": 445,
    "distinct_cves": 236,
    "rows_sha256": "13d1841850fbe1665496e888d4f8e233bf51bce2bef9835e80f8d54f712c1544",
    "canonicalization": "Sort the list of {id,range} rows by the bytes of each row serialized with UTF-8, sorted dictionary keys, ensure_ascii=False, separators=(comma,colon); serialize the sorted list in the same way, without trailing newline.",
    "bounded_0_or_1_x_affected": 443,
    "other_records": [
      {
        "id": "CVE-2007-5502",
        "range": {
          "versionType": "custom",
          "status": "affected",
          "version": "fips-1.1.1",
          "lessThan": "fips-1.1.2"
        },
        "conclusion": "separate historical FIPS Object Module lineage"
      },
      {
        "id": "CVE-2016-2183",
        "range": {
          "version": "1.1.0",
          "status": "unaffected"
        },
        "conclusion": "explicit unaffected singleton 1.1.0"
      }
    ],
    "conclusion": "outside_affected_version for this archive and OpenSSL 4.0.3 only"
  },
  "records": [
    {
      "id": "CVE-2026-107493",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "PEM_X509_INFO_read_bio_ex called with a caller-supplied stack",
      "group": "keys",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "351cea1c7b27ee1f1737b1d3091e054a372ffcec8f613748f3eda924fe17feb9",
      "record_sha256": "e6a357db75e315dd21a6b2c4a12f6704bdb9fa56fcae8b5275e662892d9568f0",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/107xxx/CVE-2026-107493.json"
    },
    {
      "id": "CVE-2026-107494",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "QUIC RETIRE_CONNECTION_ID send queue",
      "group": "quic",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "b4286370f653419262461dfc913ac09812695f531e963fcaad28a4d3be4b92a7",
      "record_sha256": "2f51c45ef4450e8eef62fb66c106430aefff686c60591cf844744e13dd692d72",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/107xxx/CVE-2026-107494.json"
    },
    {
      "id": "CVE-2026-107496",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "CMS_encrypt over caller-supplied EC groups",
      "group": "keys",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "51c8e31114262253cb31576e6ae1d28cec2372974a25544a079ac52bb8300c99",
      "record_sha256": "58169cb809ef0299bb8c420ebe0ad1dd7373634f527853d640dffa7ec696ae24",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/107xxx/CVE-2026-107496.json"
    },
    {
      "id": "CVE-2026-121482",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "X.509 certificate validation with CRLs and relative distribution point names",
      "group": "keys",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "04e0aa4d38cc51a3ee08b6109e1e1c80cb0387e6c29d0dcc610bca2daa31d550",
      "record_sha256": "7a911e9a6c2c010a01f01f8d815c87ab05d0286a0125cc5f80df6d82ffde2fdb",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/121xxx/CVE-2026-121482.json"
    },
    {
      "id": "CVE-2026-124709",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "QUIC server processing rejected streams",
      "group": "quic",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "f01787b5604af79be8615b9c9bd57d5cb8cbd893ae0b864aa736005ffd83330e",
      "record_sha256": "dc20fca48906d51a0c4b04e421a1f7ddb88fe3df38647c4d6c572df773a1bc55",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/124xxx/CVE-2026-124709.json"
    },
    {
      "id": "CVE-2026-35190",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "TLS 1.3 key share with Suite B certificate flag",
      "group": "tls",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "LOW",
        "MODERATE"
      ],
      "cna_sha256": "1567c5285fa9eaf1952294bc917e503c4bbc5b0ecb5b4fe2ee5629bfdc1d0a4b",
      "record_sha256": "f386856e797d636fae655838505b8d2ebf2e66607bfa7bb0dcb8310e637f755f",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/35xxx/CVE-2026-35190.json"
    },
    {
      "id": "CVE-2026-84785",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "QUIC stream reassembly",
      "group": "quic",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "LOW"
      ],
      "cna_sha256": "f6164fe2e2d989cfd28ba3a67333f7a8edb9b1b25f0fce64ed674983dd5ea2dc",
      "record_sha256": "c56281ae4e0683734a56fb867fa145d7fc317de7d40cf88f810a00fbea08e2f6",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/84xxx/CVE-2026-84785.json"
    },
    {
      "id": "CVE-2026-84786",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "TLS client with DANE enabled for non-SMTP",
      "group": "tls",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "ce120ba64fe21aef614f3ae58162e45b239185005a19f4da695f79a0a281e584",
      "record_sha256": "46dab8e00156a6e48c02b214cab28d6f9cc4b2ff38016fd812e54846ea5a6ada",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/84xxx/CVE-2026-84786.json"
    },
    {
      "id": "CVE-2026-88822",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "QUIC peer address detection",
      "group": "quic",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "LOW",
        "MODERATE"
      ],
      "cna_sha256": "ff90ab7e3ae590ed06abbafce6c1c9478fa0a31c8bbf56f2f40a0a45fae8d953",
      "record_sha256": "a813a69017141fe1f400e0379797d0506117423c233daebee7edda03031af241",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/88xxx/CVE-2026-88822.json"
    },
    {
      "id": "CVE-2026-88823",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "AES-GCM-SIV incremental AAD processing",
      "group": "aead",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "2769571a1937352b7c7d66f0815331da4a6604353051fda51ed5194edcc47ae5",
      "record_sha256": "afdc35c29e45f58e3c5c4ebace93171b3b89eb8dbab269758829062207c04a72",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/88xxx/CVE-2026-88823.json"
    },
    {
      "id": "CVE-2026-95691",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "QUIC CRYPTO retransmission",
      "group": "quic",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "949d6ed33fed90ea13d5daf06e85a13975043e1db4291a61da6d5f0ab6b464c9",
      "record_sha256": "2b406434d1e98e46aa37af3f8ad4e477c81f73f670aa35b35d31229f2b567b0e",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/95xxx/CVE-2026-95691.json"
    },
    {
      "id": "CVE-2026-95692",
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "prerequisite": "AES-GCM-SIV with nonstandard IV length",
      "group": "aead",
      "range": {
        "lessThan": "4.0.4",
        "status": "affected",
        "version": "4.0.0",
        "versionType": "semver"
      },
      "levels": [
        "HIGH",
        "LOW"
      ],
      "cna_sha256": "e877a92df2e34a924d023d8f634a6f54ebe45e90ff70ebb85cebc783be8f9217",
      "record_sha256": "d6cc72e7c2536a4a709d7cd5f0d0b6b96744810bf97e9e6d714dcba16e147fa6",
      "record_url": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/95xxx/CVE-2026-95692.json"
    }
  ],
  "invalidate_if": [
    "wheel, ELF, SBOM or resolved dependencies change",
    "effective unit, interpreter, product sources, adjacent producer, peer import origins or supported service profile change",
    "donor 136 ceases to meet observed absent snapshot or another target is included",
    "version range, full live upstream catalogue, external CVE bytes or candidate data changes",
    "new/unknown records or unverifiable evidence require fresh assessment"
  ],
  "installation_profiles": {
    "capture_1164445": {
      "scripts/06-alice.sh": "b18f34abb7122c128b7c8e4fe2cb9632fe7d3f2450d7cc94b81308a676779f5e",
      "scripts/06b-rules.sh": "0bd57fb5fc7a68751a0b702f041859dd8eb18c45cb3bc9fc3f0a3c70d3bb30ed",
      "scripts/lib.sh": "6104c3861a495d059f2c6a921b5726bdd27cce488902ef120697bbd08da35909"
    },
    "qualified_51d45e": {
      "scripts/06-alice.sh": "40d2ef3edd1cfb09d1a6cde9d401789d74ab11a766cc34265fb1aed00aefde47",
      "scripts/06b-rules.sh": "7a629e70cce22343a83a28d4c1e4518f4838e68522ff488d83abaaf64d244446",
      "scripts/lib.sh": "c2d2a8fdbc7f59a16412cea7363bf4e47b1a904949cb8c7b1d62505468ccd047"
    }
  }
}
```
