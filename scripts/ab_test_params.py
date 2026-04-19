#!/usr/bin/env python3
"""
Quick A/B test: re-translate 6 records with correct Gemma 4 params
and compare against old translations (wrong params: temp=0.3, top_k=300).

Run on Vast.ai:
    python scripts/ab_test_params.py
"""

import json
from openai import OpenAI
import httpx

ENDPOINT = "http://localhost:8000/v1"
MODEL = "gemma4-31b-awq"
SYSTEM = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

# Correct Gemma 4 params
NEW_PARAMS = dict(max_tokens=4096, temperature=1.0,
                  top_p=0.95, extra_body={"top_k": 64})

client = OpenAI(
    base_url=ENDPOINT, api_key="EMPTY",
    http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
)

# 6 test cases: first 3 + judge sample indices 571, 651, 834
TESTS = [
    {
        "id": 0,
        "en": [
            "Give three tips for staying healthy.",
            "1. Eat a balanced and nutritious diet: Make sure your meals are inclusive of a variety of fruits and vegetables, lean protein, whole grains, and healthy fats. This helps to provide your body with the essential nutrients to function at its best and can help prevent chronic diseases.\n\n2. Engage in regular physical activity: Exercise is crucial for maintaining strong bones, muscles, and cardiovascular health. Aim for at least 150 minutes of moderate aerobic exercise or 75 minutes of vigorous exercise each week.\n\n3. Get enough sleep: Getting enough quality sleep is crucial for physical and mental well-being. It helps to regulate mood, improve cognitive function, and supports healthy growth and immune function. Aim for 7-9 hours of sleep each night.",
        ],
        "old_da": [
            "عطيني تلاتة ديال النصائح باش تبقى بصحتك.",
            "1. كول ماكلة متوازنة ومغذية: حرص أن الوجبات ديالك يكون فيها تشكيلة من الخضر والفواكه، البروتينات قليلة الدسم، الحبوب الكاملة، والدهون الصحية. هادشي كيعاون الجسم ديالك ياخد العناصر الغذائية الأساسية باش يخدم في أحسن حالة، وكيساعد في الوقاية من الأمراض المزمنة.\n\n2. دير نشاط بدني منتظم: الرياضة ضرورية باش تحافظ على قوة العظام والعضلات وصحة القلب والشرايين. حاول دير على الأقل 150 دقيقة ديال تمارين متوسطة، أو 75 دقيقة ديال تمارين قوية كل سيمانة.\n\n3. نعس الكفاية: النعاس الكافي وبجودة عالية ضروري للصحة الجسدية والنفسية. كيعاون في تنظيم المزاج، كيحسن القدرات الذهنية، وكيساعد في النمو الصحي وتقوية المناعة. حاول تنعس ما بين 7 و 9 ديال السوايع كل ليلة.",
        ],
    },
    {
        "id": 1,
        "en": [
            "What are the three primary colors?",
            "The three primary colors are red, blue, and yellow. These colors are called primary because they cannot be created by mixing other colors and all other colors can be made by combining them in various proportions. In the additive color system, used for light, the primary colors are red, green, and blue (RGB).",
        ],
        "old_da": [
            "شنو هما تلاتة د الألوان الأساسية؟",
            "الألوان الأساسية التلاتة هي الأحمر، الزرق، والصفر. هاد الألوان كيتسماو أساسية حيت ما يمكنش نصاوبوهم من خلط ألوان أخرى، ولكن كاع الألوان الأخرى ممكن نصاوبوها يلا خلطناهم بنسب مختلفة. أما فـنظام الألوان الإضافي اللي كيستعمل فـالضو، الألوان الأساسية هي الأحمر، الخضر، والزرق (RGB).",
        ],
    },
    {
        "id": 2,
        "en": [
            "Pretend you are a project manager of a construction company. Describe a time when you had to make a difficult decision.",
            "I had to make a difficult decision when I was working as a project manager at a construction company. I was in charge of a project that needed to be completed by a certain date in order to meet the client's expectations. However, due to unexpected delays, we were not able to meet the deadline and so I had to make a difficult decision. I decided to extend the deadline, but I had to stretch the team's resources even further and increase the budget. Although it was a risky decision, I ultimately decided to go ahead with it to ensure that the project was completed on time and that the client's expectations were met. The project was eventually successfully completed and this was seen as a testament to my leadership and decision-making abilities.",
        ],
        "old_da": [
            "تخايل راسك مدير مشروع فشركة ديال البني. عاود لينا على شي مرة كان خاصك تاخد فيها قرار صعيب.",
            "كان خاصني ناخد قرار صعيب ملي كنت خدام كمسير مشاريع فشركة ديال البني. كنت مسؤول على واحد المشروع اللي كان خاصو يسالي فواحد التاريخ محدد باش نرضيو الكليان. ولكن، بسبب تعطلات ما كانتش متوقعة، ما قدرناش نحترمو الوقت، داكشي علاش كان خاصني ناخد قرار صعيب. قررت نمدد الوقت، ولكن كان خاصني نضغط على الفريق كتر ونزيد فالميزانية. وخا كان قرار فيه مخاطرة، فالاخير قررت نمشي فيه باش نضمن أن المشروع يسالي فالموعد وباش نلبّيو توقعات الكليان. فالاخير، المشروع كمل بنجاح، وهادشي كان دليل على القدرة ديالي فالتسيير واتخاذ القرارات.",
        ],
    },
    {
        "id": 571,
        "en": [
            "Explain what is foreshadowing.",
            "Foreshadowing is a literary device that authors use to give the reader hints or clues about events that will happen later in the story. Writers might use foreshadowing to create suspense, build tension, or prepare the reader for a significant event or twist in the plot.",
        ],
        "old_da": [
            "شرح ليا شنو هو التمهيد (foreshadowing).",
            "التمهيد (Foreshadowing) هو واحد التقنية أدبية كيستعملوها الكتاب باش يعطيو للقارئ تلميحات أو إشارات على أحداث غادي تطرا من بعد في القصة. الكتاب كيقدرو يستعملو التمهيد باش يخلقو التشويق، يزيدو من حدة ال",
        ],
    },
    {
        "id": 651,
        "en": [
            "Explain what a computer algorithm is.",
            "A computer algorithm is a set of step-by-step instructions that a computer follows to perform a specific task. An algorithm provides the computer with a clear and precise sequence of actions to solve a problem or complete a task.",
        ],
        "old_da": [
            "شرح ليا شنو هو الخوارزمية (algorithm) ديال الكمبيوتر.",
            "الخوارزمية ديال الكمبيوتر هي مجموعة من التعليمات اللي كيمشي عليها الكمبيوتر خطوة بخطوة باش يدير مهمة محددة. الخوارزمية كتعطي للكمبيوتر تسلسل واضح ودقيق ديال الأفعال باش يحل مشكلة ولا يكمل شي خدمة.",
        ],
    },
    {
        "id": 834,
        "en": [
            'Generate a question with the keyword "fungi"',
            "What role do fungi play in the ecosystem and how do they interact with other organisms?",
        ],
        "old_da": [
            'صاوب سؤال فيه الكلمة ديال "الفطريات" (fungi).',
            "شنو هو الدور اللي كتلعبو الفطريات في النظام البيئي وكيفاش كيتفاعلو مع الكائنات الحية الأخرى؟",
        ],
    },
]


def translate(text: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text},
        ],
        **NEW_PARAMS,
    )
    return resp.choices[0].message.content.strip()


def main():
    results = []
    for test in TESTS:
        print(f"\n{'='*60}")
        print(f"Record {test['id']}")
        print(f"{'='*60}")

        new_da = []
        for i, en_text in enumerate(test["en"]):
            role = "user" if i == 0 else "assistant"
            new_text = translate(en_text)
            new_da.append(new_text)

            print(f"\n  [{role}] EN: {en_text[:120]}...")
            print(f"  OLD (temp=0.3, top_k=300): {test['old_da'][i][:120]}...")
            print(f"  NEW (temp=1.0, top_k=64):  {new_text[:120]}...")

        results.append({
            "id": test["id"],
            "en": test["en"],
            "old_darija": test["old_da"],
            "new_darija": new_da,
        })

    # Write comparison file
    with open("output/ab_comparison.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n\nWrote comparison to output/ab_comparison.jsonl")
    print("Send this file to a judge for evaluation!")


if __name__ == "__main__":
    main()
