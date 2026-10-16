import { test } from "node:test";
import assert from "node:assert/strict";
import { cleanSpeech, xmlEscape, alertSpeech, opener, spokenMinutes } from "../src/speech.js";
import { menuTwiml } from "../src/twiml.js";
import { setup } from "./helpers.js";

test("URLs become 'link', markdown and control characters are removed", () => {
  assert.equal(cleanSpeech("see https://example.com/a?b=1&c=2 now"), "see link now");
  assert.equal(cleanSpeech("docs at www.example.org/x."), "docs at link");
  assert.equal(cleanSpeech("(see http://x.io/y)"), "(see link)");
  assert.equal(cleanSpeech("read [the runbook](https://x.io/rb) first"), "read the runbook first");
  assert.equal(cleanSpeech("**Bold** _it_ `code` # Head ~~gone~~ | a | b |"), "Bold it code Head gone a b");
  assert.equal(cleanSpeech("> quoted\n> more"), "quoted more");
  assert.equal(cleanSpeech("a\tb\r\nc\u0000d\u0007e​f"), "a b c d e f");
  assert.equal(cleanSpeech("   lots    of\n\n space  "), "lots of space");
  assert.equal(cleanSpeech("x < y && \"q\" 'z' > w"), "x < y && \"q\" 'z' > w");
});

test("message is cut to 700 characters", () => {
  const long = "word ".repeat(400);
  const out = cleanSpeech(long, 700);
  assert.ok([...out].length <= 700);
  assert.ok(out.length > 690);
  const emoji = "😀".repeat(800);
  assert.equal([...cleanSpeech(emoji, 700)].length, 700);
});

test("XML escaping covers < & > \" '", () => {
  assert.equal(xmlEscape(`<&>"'`), "&lt;&amp;&gt;&quot;&apos;");
  const twiml = menuTwiml({
    voice: "Polly.Matthew-Neural",
    speech: `Fish & chips <b> "now" it's`,
    gatherUrl: "https://h/twilio/gather?alert=al_1&attempt=1",
    redirectUrl: "https://h/twilio/voice?alert=al_1&attempt=1&pass=1",
    pass: 0,
  });
  assert.ok(twiml.includes("Fish &amp; chips &lt;b&gt; &quot;now&quot; it&apos;s"));
  assert.ok(twiml.includes('action="https://h/twilio/gather?alert=al_1&amp;attempt=1"'));
  assert.ok(twiml.includes(">https://h/twilio/voice?alert=al_1&amp;attempt=1&amp;pass=1</Redirect>"));
  assert.ok(!/&(?!amp;|lt;|gt;|quot;|apos;)/.test(twiml), "no raw & left");
});

test("spoken prompt wording (planner: no title) and opener", () => {
  assert.equal(opener("bus"), "your agents");
  assert.equal(opener("planner"), "your planner");
  assert.equal(opener("ci"), "ci");
  assert.equal(
    alertSpeech({ client: "bus", title: "Pecunia prod is down", message: "Rollback needs you", snoozeMinutes: 10 }),
    "Yo, you have to lock in on this now. Pecunia prod is down: Rollback needs you. 1 if you're on it, 2 to snooze, 9 if it's done.",
  );
  assert.equal(
    alertSpeech({ client: "planner", title: "Overdue!", message: "Due at 3 PM.", snoozeMinutes: 1 }),
    "Yo, you have to lock in on this now. Due at 3 PM. 1 if you're on it, 2 to snooze, 9 if it's done.",
  );
});

test("answer TwiML has the exact Gather shape and escaped, cleaned text", async () => {
  const h = setup();
  const { json } = await h.create({
    title: "Deploy <prod> & db",
    message: "See **https://dash.example.com/x?a=1&b=2** — it's \"red\"",
  });
  const id = json.alert.id;
  const voiceUrl = h.fetch.of("twilio.create")[0].form.get("Url");
  const r = await h.twilio(voiceUrl, { CallSid: "CAx", CallStatus: "in-progress" });
  assert.equal(r.status, 200);
  assert.match(r.headers.get("Content-Type"), /text\/xml/);
  const base = "https://agent-bus-phone.example.workers.dev";
  assert.equal(
    r.text,
    "<Response>" +
      `<Gather input="dtmf" numDigits="1" timeout="7" action="${base}/twilio/gather?alert=${id}&amp;attempt=1" method="POST" actionOnEmptyResult="false">` +
      '<Say voice="Polly.Matthew-Neural">Yo, you have to lock in on this now. Deploy &lt;prod&gt; &amp; db: See link — it&apos;s &quot;red&quot;. 1 if you&apos;re on it, 2 to snooze, 9 if it&apos;s done.</Say>' +
      "</Gather>" +
      '<Say voice="Polly.Matthew-Neural">No key pressed. I will call back in 3 minutes.</Say><Hangup/>' +
      "</Response>",
  );
});

test("CALL_INTRO opens the call; custom text is cleaned, empty means no intro; minutes are spelled out", async () => {
  assert.equal(
    alertSpeech({ intro: "Get up", client: "planner", title: "T", message: "M", snoozeMinutes: 10 }),
    "Get up. M. 1 if you're on it, 2 to snooze, 9 if it's done.",
  );
  assert.equal(spokenMinutes(1), "one minute");
  assert.equal(spokenMinutes(20), "twenty minutes");
  assert.equal(spokenMinutes(45), "45 minutes");
  assert.ok(alertSpeech({ intro: "", client: "bus", title: "T", message: "M", snoozeMinutes: 10 }).startsWith("T: M. 1 if"));
  const h = setup({ env: { CALL_INTRO: "**Study** now!" } });
  await h.create({ title: "Exam", message: "Tomorrow" });
  const voiceUrl = h.fetch.of("twilio.create")[0].form.get("Url");
  const r = await h.twilio(voiceUrl, { CallSid: "CAy", CallStatus: "in-progress" });
  assert.ok(r.text.includes(">Study now! Exam: Tomorrow. 1 if"));
});

test("calls are short: at most 20 words / 140 characters of the alert, cut at a word boundary", () => {
  const long = Array.from({ length: 40 }, (_, i) => `w${i}`).join(" ");
  const said = alertSpeech({ intro: "", client: "planner", title: "T", message: long });
  assert.equal(said, Array.from({ length: 20 }, (_, i) => `w${i}`).join(" ") + ". 1 if you're on it, 2 to snooze, 9 if it's done.");
  const chars = alertSpeech({ intro: "", client: "planner", title: "T", message: "abcdefghij ".repeat(19) });
  const body = chars.slice(0, -" 1 if you're on it, 2 to snooze, 9 if it's done.".length);
  assert.ok(body.length <= 141 && body.endsWith("abcdefghij."), body);
  // Bus: title + at most 10 words of the message.
  const bus = alertSpeech({ intro: "", client: "bus", title: "Prod down", message: long });
  assert.ok(bus.startsWith("Prod down: w0 w1 w2 w3 w4 w5 w6 w7 w8 w9. 1 if"), bus);
});
