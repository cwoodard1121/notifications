import { test } from "node:test";
import assert from "node:assert/strict";
import { cleanSpeech, xmlEscape, alertSpeech, opener } from "../src/speech.js";
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

test("spoken prompt wording and opener", () => {
  assert.equal(opener("bus"), "your agents");
  assert.equal(opener("planner"), "your planner");
  assert.equal(opener("ci"), "ci");
  assert.equal(
    alertSpeech({ client: "bus", title: "Pecunia prod is down", message: "Rollback needs you", snoozeMinutes: 10 }),
    "Urgent alert from your agents. Pecunia prod is down. Rollback needs you. Press 1 to acknowledge. Press 2 to snooze for 10 minutes. Press 3 to hear it again.",
  );
  assert.equal(
    alertSpeech({ client: "planner", title: "Overdue!", message: "Due at 3 PM.", snoozeMinutes: 1 }),
    "Urgent alert from your planner. Overdue! Due at 3 PM. Press 1 to acknowledge. Press 2 to snooze for 1 minute. Press 3 to hear it again.",
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
      '<Say voice="Polly.Matthew-Neural">Urgent alert from your agents. Deploy &lt;prod&gt; &amp; db. See link — it&apos;s &quot;red&quot;. Press 1 to acknowledge. Press 2 to snooze for 10 minutes. Press 3 to hear it again.</Say>' +
      "</Gather>" +
      `<Redirect method="POST">${base}/twilio/voice?alert=${id}&amp;attempt=1&amp;pass=1</Redirect>` +
      "</Response>",
  );
});
