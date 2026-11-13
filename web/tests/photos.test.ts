import assert from "node:assert/strict";
import { test } from "node:test";
import { MAX_PHOTOS, PHOTOS_FOUND_TEXT, cleanPhotos, photoAlt, photoImageUrl, photoSourceLabel, photosAreTheAnswer } from "../src/lib/photos";

const photo = (over: Record<string, unknown> = {}) => ({
  id: 7, document_id: 3, page_number: 214, filename: "1961-April To1962-March Part-1.pdf", year: "1961-62", caption: "किर्लोस्कर", kind: "photo", contains_face: true, ...over,
});

test("a well-formed photograph is kept as it is", () => {
  assert.deepEqual(cleanPhotos([photo()]), [photo()]);
});

test("anything that is not a list of photographs gives none", () => {
  for (const value of [undefined, null, "x", 3, {}, [], [null], ["a"], [[]]]) assert.deepEqual(cleanPhotos(value), []);
});

test("a photograph without a proper id, document or page is dropped, the others stay", () => {
  const bad = [photo({ id: 0 }), photo({ id: "7" }), photo({ id: 1.5 }), photo({ document_id: null }), photo({ page_number: -1 })];
  assert.deepEqual(cleanPhotos([...bad, photo({ id: 9 })]).map((p) => p.id), [9]);
});

test("each id appears once and no more than MAX_PHOTOS are kept", () => {
  assert.equal(cleanPhotos([photo(), photo(), photo({ id: 8 })]).length, 2);
  const many = Array.from({ length: 30 }, (_, i) => photo({ id: i + 1 }));
  assert.equal(cleanPhotos(many).length, MAX_PHOTOS);
});

test("odd fields are made safe: a missing caption is empty, an unknown kind is a photo, a long caption is cut, spaces are tidied", () => {
  const [p] = cleanPhotos([photo({ caption: undefined, kind: "weird", year: "", contains_face: "yes" })]);
  assert.equal(p.caption, "");
  assert.equal(p.kind, "photo");
  assert.equal(p.year, null);
  assert.equal(p.contains_face, false);
  assert.equal(cleanPhotos([photo({ caption: "a ".repeat(400) })])[0].caption.length <= 300, true);
  assert.equal(cleanPhotos([photo({ caption: "  Dr.   Rao \n at the  fair " })])[0].caption, "Dr. Rao at the fair");
});

test("extra fields from the server (a score, a link) are not carried into the chat", () => {
  const [p] = cleanPhotos([photo({ score: 0.9, signed_url: "https://x/y?token=secret" })]);
  assert.equal("signed_url" in p, false);
  assert.equal("score" in p, false);
});

test("a picture is shown through this app's own route, never a link that expires", () => {
  assert.equal(photoImageUrl(7), "/api/photos/7");
});

test("the alternative text is the caption, or says where the photograph is from", () => {
  assert.equal(photoAlt(cleanPhotos([photo()])[0]), "किर्लोस्कर");
  assert.equal(photoAlt(cleanPhotos([photo({ caption: "" })])[0]), "Photograph on page 214 of Sampada 1961-62");
  assert.equal(photoAlt(cleanPhotos([photo({ caption: "", year: null })])[0]), "Photograph on page 214 of Sampada");
});

test("the line under a photograph says its page and issue", () => {
  assert.equal(photoSourceLabel(cleanPhotos([photo()])[0]), "Page 214 · Sampada 1961-62");
  assert.equal(photoSourceLabel(cleanPhotos([photo({ year: null })])[0]), "Page 214");
});

test("the photographs are the answer when the text search found nothing but photographs were found", () => {
  const photos = cleanPhotos([photo()]);
  assert.equal(photosAreTheAnswer({ found: false, photos }), true);
  assert.equal(PHOTOS_FOUND_TEXT, "Photos found in Sampada archive:");
});

test("they are not the answer when the text answer was found, or there are no photographs", () => {
  const photos = cleanPhotos([photo()]);
  assert.equal(photosAreTheAnswer({ found: true, photos }), false); // a real answer, with photographs beside it
  assert.equal(photosAreTheAnswer({ found: false, photos: [] }), false); // an ordinary "not found"
  assert.equal(photosAreTheAnswer({ found: false }), false);
  assert.equal(photosAreTheAnswer({ photos }), false); // an old chat that never recorded "found"
});

test("a question back stays a question, even with photographs", () => {
  assert.equal(photosAreTheAnswer({ found: false, clarify: true, photos: cleanPhotos([photo()]) }), false);
});
