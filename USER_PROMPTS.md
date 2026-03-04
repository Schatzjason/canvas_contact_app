# User Prompts

Prompts 1–13 are reconstructed from a session-context summary (earlier conversation ran out of context window). Exact wording may differ slightly from the original. Prompts 14 onward are verbatim from the current session.

---

1. Read CLAUDE_CODE_PROMPT_v2.md then survey the current directory structure. Tell me what you see and confirm your plan before writing any code. I'd like to proceed in small steps.

2. That sounds right.

3. Yes. Sorry for changing the prompt while work was underway. But the new prompt is correct. We'll delay OAuth for phase 2. Your proposals sound good.

4. I just added a CLAUDE.md file with some permissions info. Read that and let me know if it makes sense.

5. Yes. *(confirming to add `flask db downgrade` to the always-ask list)*

6. Okay. Let's proceed.

7. I'd like to see how the steps 2 and 3 work. Walking the conversations and discussions. Which files are those in?

8. That is the expected behavior.

9. I'd like to show the user progress information while they are waiting for the course dashboard to open. *(full SSE progress-streaming request)*

10. Add a small flush cache button on the course page. Just for testing purposes. I'd like to be able to see that downloading feedback.

11. When messages are being fetched, make sure we are only getting messages in the last 21 days. That may be already happening.

12. For the progress feedback section on the index page, is it possible to show numbers as conversations are fetched? Or do they all get fetched at once.

13. First remove all of the "Reading ..." updates. Those happen too fast to see.

---

*(Session context was compacted here; prompts below are verbatim.)*

---

14. Now, in the same feedback section in the index page add a single dot to the "fetching conversateions..." each time any progress is made in fetching messages. So that it would first be "fetching conversations..." and then "fetching conversations...." etc. Add a dot when a page of conversations is fetched. Or any other progress that might be used.

15. Can you make a document with every prompt that I have provided?
