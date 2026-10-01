// TwiML builders. All text and attribute values go through xmlEscape (so & in URLs becomes &amp;).
import { xmlEscape as x } from "./speech.js";

function say(voice, text) {
  return `<Say voice="${x(voice)}">${x(text)}</Say>`;
}

/**
 * The keypad menu. pass 0: Gather(Say) then Redirect to pass 1.
 * pass 1: Gather(Say) then the goodbye line and Hangup.
 */
export function menuTwiml({ voice, speech, gatherUrl, redirectUrl, pass, goodbye }) {
  const gather =
    `<Gather input="dtmf" numDigits="1" timeout="7" action="${x(gatherUrl)}" method="POST" actionOnEmptyResult="false">` +
    say(voice, speech) +
    `</Gather>`;
  const tail = pass === 0 ? `<Redirect method="POST">${x(redirectUrl)}</Redirect>` : `${say(voice, goodbye)}<Hangup/>`;
  return `<Response>${gather}${tail}</Response>`;
}

export function sayHangupTwiml(voice, text) {
  return `<Response>${say(voice, text)}<Hangup/></Response>`;
}

export function rejectTwiml() {
  return `<Response><Reject/></Response>`;
}

/** Reply to an inbound SMS. */
export function messageTwiml(text) {
  return `<Response><Message>${x(text)}</Message></Response>`;
}

export function emptyTwiml() {
  return `<Response/>`;
}
