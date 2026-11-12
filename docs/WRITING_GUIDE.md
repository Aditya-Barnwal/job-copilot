# Writing guide: cover letters, application emails, open answers

These rules are what `jobcopilot/writing/` and `jobcopilot/answers.py` enforce.
They come from career-office guidance (sources at the end), adjusted for
software-engineering roles and for a generator that may only use **confirmed
facts**. Every draft is shown to you before use; nothing is sent or submitted.

## Principles

1. **Short beats complete.** The letter sits next to the résumé and shouldn't
   repeat it. Keep it to one page at most. For SWE roles aim for **130–170 words
   of body, 180 max** (`config/writing.yaml: cover_letter.max_words`). Short-letter
   guides land at 150–250 words, and engineering readers skim.
2. **Structure** (MIT CAPD, Harvard OCS, Stanford worksheet):
   - *Opening*: name the role, plus one specific thing from the posting that your
     work connects to. Say how you found it only if it's a referral or event.
   - *Evidence*: 1–2 paragraphs. Map two or three of the JD's must-haves to
     concrete results, citing at least one fact for each. Choose the facts that
     answer the posting rather than summarising the résumé.
   - *Close*: one or two sentences on what you'd like to discuss, then thanks.
     Restate your interest without grovelling.
3. **Specific about them, honest about you.** Company statements may come
   only from the JD or the company record; nothing is invented about funding,
   customers or news. Applicant statements may come only from cited, confirmed
   facts. A requirement you can't back is left out, not hedged.
4. **Numbers stay as written.** "200+", "30%": a reworded metric counts as a
   new metric.
5. **Salutation.** Use "Dear Hiring Manager," unless a name is known. Never use
   "To whom it may concern".
6. **No filler.** The denylist (`writing/style.py: CLICHES`) covers lines like
   *I am writing to express my interest*, *passionate about*, *team player*,
   *perfect fit*, *proven track record*, *hit the ground running* and *world-class*.
   Hiring-manager surveys and career offices both single out the stock opener as
   the most damaging line. The generator deletes any sentence that contains one
   and tells you.
7. **No machine tells.** Leak phrases (*As an AI*, *Here is your draft*,
   `[Company]`, `{name}`, fact ids) trigger a fresh retry, and a draft that
   still has them is marked `needs_review`.

## Application emails

- **Subject**: role + your name, for example `Application: Backend Engineer at
  Initrode — Your Name`. Add the requisition ID if the posting has one. Keep it
  under about 80 characters.
- **Body**: 3–5 sentences, at most 120 words. Give the role and your single most
  relevant result, add one more result tied to a must-have, and say the résumé
  is attached. Close with the signature from `answers.yaml: writing.signoff`
  (with `{phone}` / `{linkedin}` filled in).

## Follow-ups

- **When**: 1–2 weeks after applying unless the posting gives a timeline
  (Indeed). The app warns if fewer than 7 days have passed since `submitted_at`.
- **How often**: follow up **once**. If there's no reply, move on.
- **Content**: at most 90 words. The opening line with the date you applied is
  written by the code, from the tracker. Then one relevant fact and a polite
  question about next steps. Subject: `Following up: <Role> application — <Name>`.

## Open form questions ("Why us?", "Tell us about a project")

These use the same grounding as letters and stay within the form's word or
character limit (≤ 150 words by default). They are always marked
**needs you**. The text is a draft for you to edit and is never autofilled
as-is.

## Grounding (fail closed)

Each sentence is checked against the facts its paragraph cites (a cited
bullet brings in its role or project header). Sentences that aren't about you
may also draw on the JD and the company record. The check flags a sentence for
any of these:

- a skill, number, title or ownership claim, or proper noun that its evidence
  doesn't support
- a cited id that isn't a confirmed fact

Any flag sets the status to `needs_review`. If you have no confirmed facts,
nothing is generated (`refused`).

## Example skeletons (original, for shape only)

**Cover letter (~150 words)**

> Dear Hiring Manager,
>
> The Backend Engineer posting describes moving a monolith's billing logic into
> Go services. That is the kind of migration I have been doing for the past year.
>
> At ⟨employer⟩ I built ⟨service⟩ handling ⟨metric from fact⟩, which covers the
> posting's ⟨must-have 1⟩ and ⟨must-have 2⟩.
>
> I also ⟨second fact, with its number unchanged⟩, which is close to the
> ⟨must-have 3⟩ work your team lists.
>
> I'd be glad to talk through how I'd approach ⟨JD problem⟩. Thank you for
> reading.
>
> Best regards, ⟨name⟩

**Application email (~80 words)**

> Subject: Application: Data Engineer — ⟨Name⟩
>
> Hello ⟨Company⟩ team,
>
> I'm applying for the Data Engineer role. At ⟨employer⟩ I ⟨fact matching the
> top must-have⟩. I've also ⟨fact matching a second must-have⟩. My résumé is
> attached, and I'm happy to share more detail on either.
>
> ⟨signature⟩

**Follow-up (~60 words)**

> I applied for the ⟨Role⟩ role on ⟨date⟩ and wanted to follow up. My work on
> ⟨fact⟩ lines up with ⟨JD need⟩. Is there an update, or anything else I can
> send?

## Sources

- MIT CAPD, "How to write an effective cover letter (with samples)":
  https://capd.mit.edu/resources/how-to-write-an-effective-cover-letter/
  (one page, opening / 2–3 body paragraphs / close, tailor each letter,
  "Dear Hiring Manager" when no name is known)
- Harvard FAS Mignone Center (OCS), "HES: Create impactful resumes and cover
  letters": https://careerservices.fas.harvard.edu/resources/hes-create-impactful-resumes-and-cover-letters/
  (opening states the role and how you heard; middle connects your story to
  the position)
- Stanford Career Education, "Cover Letter Worksheet":
  https://careered.stanford.edu/sites/g/files/sbiybj22801/files/media/file/cover_letter_handout.pdf
  (paragraph-by-paragraph prompts)
- Resume.io, "Short cover letter samples": https://resume.io/blog/short-cover-letter-samples
  (150–250 words; avoid the "I am writing to apply" opener)
- Resume Optimizer Pro, "Software engineer cover letter examples":
  https://resumeoptimizerpro.com/blog/software-engineer-cover-letter-examples
  (hiring-manager survey figures on generic openers)
- Indeed Career Guide, "How to write a follow-up email after a job application":
  https://www.indeed.com/career-advice/finding-a-job/follow-up-email-after-application
  (wait 1–2 weeks, clear subject line, follow up once)
