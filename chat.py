import os
from dotenv import load_dotenv
from google import genai


SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.
Tone: helpful, factual, concise.
Only answer using the uploaded docs.
Max 5 bullet points; else link to the doc.
Cite up to 3 "Article URL:" lines per reply.
"""


def stream_answer(client, model, store_name, question):
    stream = client.interactions.create(
        model=model,
        input=f"""{SYSTEM_PROMPT}

User question:
{question}
""",
        stream=True,
        tools=[
            {
                "type": "file_search",
                "file_search_store_names": [store_name],
            }
        ],
    )

    print("\nOptiBot:")
    full_answer = ""

    for event in stream:
        if event.event_type == "step.delta":
            if event.delta.type == "text":
                text = event.delta.text
                print(text, end="", flush=True)
                full_answer += text

        elif event.event_type == "error":
            print("\nStream error:")
            print(event.error)

    print()
    return full_answer


def main():
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    store_name = os.getenv("GEMINI_FILE_SEARCH_STORE_NAME")
    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

    if not api_key:
        print("Missing GEMINI_API_KEY in .env")
        return

    if not store_name:
        print("Missing GEMINI_FILE_SEARCH_STORE_NAME in .env")
        return

    client = genai.Client(api_key=api_key)

    print("OptiBot Console Chat")
    print("Streaming mode: ON")
    print("Type 'exit' to quit.")
    print("-" * 50)

    while True:
        question = input("\nYou: ").strip()

        if question == "":
            print("Please enter a question.")
            continue

        if question.lower() == "exit":
            print("Bye!")
            break

        try:
            stream_answer(
                client=client,
                model=model,
                store_name=store_name,
                question=question,
            )

        except Exception as error:
            print("\nError:")
            print(error)


if __name__ == "__main__":
    main()