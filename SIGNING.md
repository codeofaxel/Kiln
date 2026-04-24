# Signing, SBOM, and Release Verification

Kiln ships every release with four cryptographic artifacts so any
verifier — a procurement officer, a regulated-industry auditor, an
air-gapped operator — can answer "is this the real thing?" without
needing to trust me personally, trust GitHub alone, or trust PyPI
alone.

This is the copy-pasteable reference.  If you only read one section,
read **"Quick verify (one command)"** at the bottom.

## What we sign, and with what

| Artifact | Tool | Where | Verifies |
|---|---|---|---|
| Wheel + sdist | [Sigstore](https://sigstore.dev) (keyless) | `*.sigstore` bundle on the GitHub release **and** on the PyPI page | Built by this repo's CI from a specific commit SHA.  No long-lived key. |
| Wheel + sdist | [SLSA v1.0](https://slsa.dev) build provenance | GitHub attestations API + rekor public log | Builder identity, runner OS, source repo, commit SHA, workflow hash.  NIST SSDF PS.3.2 / EO 14028 §4(e). |
| Source | [CycloneDX](https://cyclonedx.org) JSON + [SPDX](https://spdx.dev) JSON | `sbom.cdx.json` / `sbom.spdx.json` on the release | Full dep tree.  Consumed by procurement / vuln-scanner tooling. |
| Git tag | GPG signature (`git tag -s`) | `.asc` on the tag itself | Maintainer identity at tag-cut time. |

## Maintainer public key

The GPG key used to sign git tags is published at:

- **`.well-known/maintainer.pub.asc`** (in this repo)
- **GitHub profile** of `codeofaxel` (keys tab)
- **`openpgp.org`** keyserver (search `security@kiln3d.com`)

The fingerprint is also in `.well-known/maintainer.pub.sha256` so an
operator behind a firewall can copy-paste it over an out-of-band
channel.  If all three sources agree, you have the right key.

> For Sigstore-signed wheels you do NOT need the maintainer key.  The
> Sigstore certificate is rooted in the GitHub OIDC token issued to
> the release workflow; the verifier only needs the repo + workflow
> identity.

## Quick verify (setup)

```bash
pip install sigstore
# optional but recommended:
brew install cosign gh syft
```

Then run the four manual checks below.  (A scripted one-liner
`verify_release.sh` exists in the kiln-pro private repo; for this
public repo, the copy-pasteable commands below are authoritative
and are all you need.)

## Manual verification — by artifact

### Verify the wheel (Sigstore, keyless)

```bash
VERSION=0.5.0
REPO=codeofaxel/Kiln
WHEEL="kiln3d-${VERSION}-py3-none-any.whl"

# Download from the GitHub release
gh release download "v${VERSION}" -R "${REPO}" -p "${WHEEL}" -p "${WHEEL}.sigstore"

# Verify
python -m sigstore verify identity \
  --cert-identity-regexp \
  "https://github.com/${REPO}/\.github/workflows/(publish|sign-release)\.yml@.*" \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle "${WHEEL}.sigstore" \
  "${WHEEL}"
```

PASS means: the wheel was built by our release workflow at some
commit SHA pushed at the time of signing, anchored in Sigstore's
public Fulcio root.  (PyPI also serves the same `.sigstore` bundle;
look for the "Provenance" link on the release's PyPI page.)

### Verify SLSA build provenance

```bash
gh attestation verify "${WHEEL}" \
  --owner codeofaxel \
  --predicate-type https://slsa.dev/provenance/v1
```

Output includes the source commit SHA and workflow file hash —
cross-reference against this repo.

### Verify the SBOMs

```bash
gh release download "v${VERSION}" -R "${REPO}" \
  -p sbom.cdx.json -p sbom.spdx.json

gh attestation verify sbom.cdx.json \
  --owner codeofaxel \
  --predicate-type https://cyclonedx.org/bom

# Inspect with your vuln scanner of choice
grype sbom:sbom.cdx.json          # Anchore Grype
trivy sbom sbom.cdx.json          # Aqua Trivy
```

### Verify the signed git tag

```bash
git clone https://github.com/codeofaxel/Kiln.git
cd Kiln

# Import the maintainer key (one-time)
curl -sSfL https://raw.githubusercontent.com/codeofaxel/Kiln/main/.well-known/maintainer.pub.asc \
  | gpg --import

git verify-tag "v${VERSION}"
```

## Threat model — what each artifact defends against

| Threat | Mitigation |
|---|---|
| Attacker uploads a malicious wheel to PyPI under our name | Wheel `.sigstore` bundle fails `cert-identity-regexp` — wasn't built by our workflow. |
| Attacker compromises a maintainer laptop and cuts a tag | `git verify-tag` fails — they don't have the GPG key. |
| Attacker compromises the GPG key but not CI | Wheel signatures still verify against CI identity; SLSA provenance ties the wheel to the actual commit, exposing mismatch. |
| Attacker compromises a third-party action | Every action is pinned to a full commit SHA (see `.github/workflows/*.yml`).  A compromised tag push cannot reach our build. |
| Attacker swaps an SBOM on the release page | `gh attestation verify` fails — SBOM attestation is in rekor's append-only transparency log. |
| Downgrade attack (re-ship old CVE wheel under new tag) | SLSA provenance includes the commit SHA; verifier catches mismatch. |

## Maintainers — cutting a release

The canonical release flow is:

1. Bump `kiln/pyproject.toml` version + `CHANGELOG.md`.
2. Create a GPG-signed annotated tag:  `git tag -s vX.Y.Z -m 'Kiln X.Y.Z'`.
3. Push the tag:  `git push origin vX.Y.Z`.
4. Create the GitHub release:  `gh release create vX.Y.Z --verify-tag --generate-notes`.
5. CI (`publish.yml`) builds, SBOMs, signs, attests, and uploads to
   PyPI in one workflow.  If any step fails, the PyPI upload is
   blocked — better nothing than unverified.

A pre-flight script lives in the `kiln-pro` private companion repo
(`scripts/sign_release.sh`) for maintainers who want to run the
build + SBOM + sign locally before advertising the tag.  This
public repo treats CI as the source of truth.

## Questions / reporting issues

Supply-chain concerns: `security@kiln3d.com` (see
`.well-known/security.txt` for RFC 9116 contact info).
