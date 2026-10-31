# YouTube transcript panel: spike (2026-10-31)

Jan asked on 2026-10-31 for a browser fallback that reads YouTube's transcript panel when
youtube-transcript-api and yt-dlp give no track (package 6, D1 option c), without an upstream
proxy (D1 option b stays out). Before planning it, a spike ran on Jan's PC (residential line,
Germany) from a fresh clone of `main` at 93ae4c2: Playwright's Chromium 156 headless, a new
context per video, locale `de-DE`, no login, no stored cookies. It observed only the page's own
requests.

## Videos

Four with captions (`uN1Zx-Ia-SE` manual de, `5At8hsfPHqU` manual de with chapters,
`HPjZDlQexeY` automatic de, `XAgqWLNlseg` automatic de, CC BY) and one with subtitles disabled
(`6g6X353YgKk`). Each ran twice: with the service's user agent (Chromium's own plus the product
token) and with the plain headless one. The two runs did not differ.

## Findings

- **Consent.** No redirect to `consent.youtube.com`. An in-page dialog
  (`ytd-consent-bump-v2-lightbox` with an inner `tp-yt-paper-dialog`) appeared every time;
  `button:has-text("Alle ablehnen")` dismissed it and set the `SOCS` cookie. Until then an
  overlay backdrop intercepts every click. No bot wall, `playabilityStatus` OK.
- **The button.** "Transkript anzeigen" (`ytd-video-description-transcript-section-renderer
  button`) is visible only after the description is expanded (`tp-yt-paper-button#expand`).
  A video without captions has no button and no transcript engagement panel.
- **The classic panel fails.** For the three videos that got it, the page's own
  `POST /youtubei/v1/get_transcript` (with `context.request.attestationResponseData`, a BotGuard
  attestation) answered `400 FAILED_PRECONDITION` ("Precondition check failed."), and the panel
  stayed empty. The user agent made no difference.
- **The new panel works.** For `XAgqWLNlseg` the same button opened YouTube's new view
  (`panelId` `PAmodern_transcript_view`): `POST /youtubei/v1/get_panel` answered 200 without an
  attestation, with 37 segments, in about 8 s end to end. The text is in
  `content.engagementPanelSectionListRenderer.content.sectionListRenderer.contents[0]
  .itemSectionRenderer.contents[i].macroMarkersPanelItemViewModel.item.timelineItemViewModel
  .contentItems[0].transcriptSegmentViewModel.simpleText`, the times in milliseconds in the
  parallel list `frameworkUpdates.entityBatchUpdate.mutations[0].payload
  .timedMarkersListSyncEntity.timedListData.sections[0].timedSyncDataList[i]` (`videoTimeMs`,
  `endTimeMs`, same index). The DOM shows `transcript-segment-view-model` elements. Whether a
  video gets the new view shows beforehand: `ytInitialData` contains `PAmodern_transcript_view`.
  It came for one of the four captioned videos, in both runs.

## Not done

A second round would have tried other browser setups (Chromium's new headless mode, a wait for
BotGuard, the stealth browser of unblock mode) to get the classic panel past the attestation.
That is getting past a site's bot protection, which CLAUDE.md rule 6 allows only for sites that
allowed extraction by contract (unblock mode); YouTube is not one. It was not run.

## Consequence

An honest fallback can use only the new panel, which YouTube served for one video in four, and
from a residential line youtube-transcript-api already gives most tracks (8 of 9 in the
package 6 spike). Jan decides whether to build it now or when YouTube serves the new panel
widely; D1 (a) stays as built until then.
