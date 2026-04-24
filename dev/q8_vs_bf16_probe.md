# Q8 GGUF vs bf16 translation probe

Seed: 42  |  Samples: 13  |  LM Studio: `tiny-aya-darija-v5`  |  Space: `Lyte/Moroccan-Darija-Translator`

**Latency:** Q8 total 121.0s (avg 9.3s/req), bf16 total 47.8s (avg 3.7s/req)


## 1. `finephrase/faq` — en len 1178

**English:**

> Colored map compiled by merging geologic and topographic maps of the United States through computer processing and enhancement with links to panoramic movie using QuickTime, descriptions of features, and a game entitled Puzzle of Regions.
The USGS provides map-on-demand (MOD) printing for selected infrequently requested geologic and other theme map products providing high-quality paper-copy maps as needed. Site links to available map products with thumbnail views and how to order.
This publication (Open-File Report 95-479) is a teacher's guide to two U.S. Geological Survey maps that show gravity and magnetic data in contour form superimposed on a LANDSAT satellite image of the San Francisco Bay area.
Techniques of computer-based representations of areal geology to construct three-dimensional geologic maps that retain all the information of traditional geologic maps while providing a more detailed picture of the subsurface.
Main page for accessing links for information and data on the San Francisco Bay estuary and its watershed with links to highlights, water, biology, wetlands, hazards, digital maps, geologic mapping, winds, bathymetry and overview of the Bay.


**A — Q8 GGUF** (9.8s, len 1042)


> خريطة ملونة مجمعة من خرائط جيولوجية ونقطة ديال المناظر الطبيعية ديال ميريكان، عن طريق الكمبيوتر باش تحسن وتزيد روابط لفيديو بانورامي بـ QuickTime، ووصف للمميزات، وواحد اللعبة سميتها Puzzle of Regions.
USGS كتقدم خدمة طباعة الخرائط على الطلب (MOD) لبعض المنتجات الجيولوجية اللي كيطلبوها بزاف، وبجودة عالية كيفما بغاو. السيت فيه روابط لمنتجات خرائطية موجودة، مع صور صغيرة وتوضيح كيفاش تطلبها.
هاد المنشور (Open-File Report 95-479) هو دليل للمعلمين على جوج خرائط من الـ U.S. Geological Survey اللي كيبينو بيانات الجاذبية والمغناطيسية على شكل خطوط متصلة فوق صورة ديال القمر الصناعي LANDSAT لمنطقة خليج سان فرانسيسكو.
تكنيكيات الكمبيوتر باش يمثلو الجيولوجيا المساحية كتعطي خرائط جيولوجية ثلاثية الأبعاد، اللي كتحافظ على كاع المعلومات ديال الخرائط التقليدية ولكن كتعطي صورة أدق على ما تحت الأرض.
الصفحة الرئيسية فيها روابط لمعلومات وبيانات على مصب خليج سان فرانسيسكو والمنطقة المصبية (basin) ديالو، مع روابط لأهم النقط، والماء، وعلم الأحياء، والمناطق الرطبة، والمخاطر، والخرائط الرقمية، والتخطيط الجيولوجي، والرياح، والعمق السطحي، وملخص على الخليج.


**B — bf16 full precision** (14.9s, len 1069)


> خريطة ملونة تجمعات من خرائط جيولوجية ونظرية ديال المناطق ديال الولايات المتحدة، تخدمات بالكمبيوتر وتزادت عليها روابط لفيديو بانورامي بـ QuickTime، ومعاهم وصف للمميزات، وواحد اللعبة سميتها "بازل" على شكل مناطق.
USGS كتقدم خدمة طباعة الخرائط على الطلب (MOD) لبعض المنتجات الجيولوجية والمناطق الأخرى اللي كيتطلبو مرة فمرة، وكتوصل خرائط ورقية بجودة عالية كيفما بغا الزبون. السيت فيه روابط لمنتجات خرائطية أخرى مع صور صغيرة (thumbnails) وطريقة الطلب.
هاد المنشور (Open File Report 95-479) هو دليل للتلاميذ باش يشوفوا جوج خرائط من المعهد الجيولوجي الأمريكي (U.S. Geological Survey)، اللي كيبينو بيانات الجاذبية والمغناطيسية على شكل خطوط متصلة فوق صورة بالأقمار الصناعية لـ خليج سان فرانسيسكو.
تكنيكيات الكمبيوتر استعملوها باش يرسموا خرائط جيولوجية ثلاثية الأبعاد، اللي كتحافظ على جميع المعلومات ديال الخرائط التقليدية ولكن كتعطي صورة أدق على تحت الأرض.
الصفحة الرئيسية فيها روابط لمعلومات وبيانات على مصب خليج سان فرانسيسكو والمنطقة المائية ديالو، مع روابط لأهم النقط، الما، البيولوجيا، المناطق الرطبة، المخاطر، الخرائط الرقمية، التضاريس الجيولوجية، والرياح، وعرض عام للخليج.


## 2. `fineweb-edu` — en len 1200

**English:**

> Learn something new every day More Info... by email
A predicate is part of a sentence or clause in English and is one of two primary components that serves to effectively complete the sentence. Sentences consist of two main components: subjects and predicates. Subjects are the primary “thing” in a sentence which the rest of the words then describe through either a direct description or by indicating what type of action that subject is performing. The predicate is this secondary aspect of the sentence and usually consists of a verb or adjective, though complicated sentences may have multiple verbs and a number of descriptions affecting the subject.
It can be easiest to understand predicates by first understanding subjects and how sentences are constructed. A sentence just about always has a subject, though it can be implied in some way and not necessarily directly stated. In a simple sentence like “The cat slept,” the subject is “the cat,” which is a noun phrase consisting of the direct article “the” and the noun “cat.” Subjects can be longer and more complicated, but they are usually fairly simple in nature.
The predicate of a sentence is then basically the rest of the sentence, th


**A — Q8 GGUF** (7.6s, len 886)


> الخبرة (Predicate) هي جزء من الجملة ولا العبارة فـ الإنجليزية، وهي وحدة من جوج أجزاء أساسية اللي كتسالي الجملة بطريقة فعالة. الجمل كيكونوا عبارة عن جوج ديال الأجزاء الأساسية: الفاعل والأخبار. الفاعل هو "الحاجة" الرئيسية فـ الجملة اللي كيتوصف عليها باقي الكلمات إما بوصف مباشر ولا بتوضيح شنو هي العملية اللي كيديرها هاد الفاعل. الخبرة هي هاد الجانب الثاني من الجملة، وعادة كتكون عبارة على فعل ولا اسم وصفي، وخا تقدر تكون جمل معقدة فيها بزاف ديال الأفعال ووصفات مختلفة كأثروا على الفاعل.
باش تفهم الخبرات بسهولة، خاصك أولا تفهم الفاعلات وكيفاش كيتكونو الجمل. أي جملة عندها فاعل، واخا يقدر يكون غير مضبوط بطريقة غير مباشرة وماشي ضروري يكون مكتوب. فـ جملة بسيطة بحال "The cat slept"، الفاعل هو "the cat"، اللي هي عبارة اسمية فيها أداة التعريف "the" والاسم "cat". الفاعلات كيقدروا يكونوا طوال ومعقدين، ولكن غالباً كيكونوا بساط بزاف. إذن الخبرة ديال الجملة هي ببساطة داكشي اللي بقى من الجملة.


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 3. `fineweb-edu` — en len 1200

**English:**

> You are browsing the web and suddenly come across a page that displays numbers
instead of letters? You see four numbers inside a little box instead of letters? Well then you might just be missing a Unicode font. Here is a simple fix.
Those four numbers inside the little box are the hexadecimal code for the letter. If you have not got the font that translates those four numbers into a character or glyph you will only be able to see the number.
Here you can find a list of all Unicode fonts as well as a comparison of those Unicode fonts.
Having a look at the list, "GNU Unifont" clearly stands out with 63,446 characters 63,449 glyphs. As the name already suggests GNU Unifont is free to use and to download. You can get the GNU Unifont for Mac and Windows from here. Download the WinZip archive for Mac or Windows, it is 3MB in file size.
Once downloaded simply unzip the file and copy it to your systems fonts folder.
If you have trouble with that have a look here for Windows 7 users and here for Windows XP users. Basically in Windows the fonts are located at c:\windows\fonts.
Mac users can check this out to install the font.
Additionally here is a link to the Unicode 6.0 Character Code Cha


**A — Q8 GGUF** (9.9s, len 1112)


> إلا كنتي كتصفح الأنترنيت ولقيتي شي صفحة فيها أرقام بلاصة الحروف، ولا شفتي أربعة ديال الأرقام وسط واحد الصندوق صغير بلاصة الحروف، فهاد الحالة تقدر تكون غير ناقصك شي "فونت" (font) من نوع Unifont. ها هو الحل ساهل وبسيط: هادوك الأربعة ديال الأرقام اللي كاينين وسط الصندوق هما الكود الست عشري (hexadecimal code) ديال حرف معين. إذا ما كانش عندك داك الفونت اللي كيرد هادوك الأرقام حرف، غادي تشوف غير الرقم بوحدو. هنا تقدر تلقى ليستة ديال كاع فونطات Unifont ومقارنة بيناتهم. ومن بعد ما تشوف اللائحة، غادي تبان ليك "GNU Unifont" بوضوح بـ 63,446 حرف و 63,449 نقطة (glyph). وكيفما كيقول الاسم، GNU Unifont مجانية وفيها تقدر تستعملها وتتيليشارجيها. تقدر تاخد GNU Unifont ديال Mac و Windows من هنا. تيليشارجي الأرشيف WinZip ديال Mac ولا Windows، الحجم ديالو غي 3 ميجا. ومن بعد ما تيليشارجي، غير فك الملف (unzip) وعطيه للنسخة ديال الفونطات اللي عندك في السيستيم. وإلا كان عندك مشكل مع هادشي، شوف هنا للمستخدمين ديال ويندوز 7 وهنا للي خدامين بـ Windows XP. وبصفة عامة، فـ Windows الفونطات كيكونوا فـ c:\windows\fonts. ومستخدمي Mac يقدروا يشوفو هادشي باش يركبو الفونت. وزيد عليها هادي هي الرابط لـ Unicode 6.0 Character Code Cha


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 4. `finephrase/tutorial` — en len 1200

**English:**

> The Government of Pakistan has always been committed to providing quality education to little ones of Pakistan.
As far as the present educational scenario is concerned, Pakistan’s schooling system is said to be under-funded, under-equipped and under-staffed at a time when 60 percent of its population is under 18.
Going by the statistics of United Nation Pakistan's government spends -2.4 percent of its gross domestic product, compared with the United Nations' recommended norm of 4percent in developing economies like Pakistan.
The new Pakistani Government has vowed to work on all the greyare as concerning the schooling scenario in Pakistan
The educational system of Pakistan is among the least-developed in the world. The system was based on the British colonial educational system, which lasted until 1947. In that year, Pakistan gained independence as a result of the partition of the Indian subcontinent into the states of India and Pakistan. The colonial system was elitist; it was meant to educate a small portion of the population to run the government. Despite changes since independence, the Pakistani educational system has retained its colonial elitist character, a factor preventing 


**A — Q8 GGUF** (7.9s, len 991)


> الحكومة الباكستانية ديما كانت ملتزمة باش توفر تعليم مزيان للأطفال الصغار فالباكستان.
من ناحية الوضع التعليمي الحالي، كيقولو بلي النظام المدرسي فالباكستان ناقص من حيث الميزانية، والمعدات، وعدد الأساتذة، وهادشي في وقت اللي 60% من السكان تحت سن 18 عام.

على حساب المعطيات ديال الأمم المتحدة، الحكومة الباكستانية كتصرف -2.4% من الناتج المحلي الإجمالي، مقارنة مع المعدل اللي كينصحو بيه الأمم المتحدة ديال 4% فالاقتصادات النامية بحال الباكستان.
الحكومة الباكستانية الجديدة تعهدات باش تخدم على جميع الجوانب اللي ناقصة في التعليم فالباكستان.

النظام التعليمي فالباكستان من بين أقل الأنظمة تطوراً فالعالم. هاد النظام مبني على نظام تعليمي بريطاني كان هو اللي ساد حتى لـ 1947. فديك العام، الباكستان خدات الاستقلال ديالها نتيجة التقسيم ديال شبه القارة الهندية لولايات الهند والباكستان. كان هاد النظام استعماري وكيهدف غير لتكوين طبقة صغيرة من المجتمع باش يسيروا الدولة. ورغم التغييرات اللي وقعات من بعد الاستقلال، بقى النظام التعليمي الباكستاني محافظ على الطابع الاستعماري ديالو، وهذا هو السبب اللي كيمنع


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 5. `fineweb-edu` — en len 1200

**English:**

> |born on||11 March 1611 at 12:00 (= 12:00 noon )|
|Place||Southwick, England, 50n50, 0w13|
|Timezone||LMT m0w13 (is local mean time)|
|Astrology data||20°32' 15°14 Asc. 17°30'|
British scientist, a brilliant linguist and mathematician. He was the son of a non-conformist clergyman who died when the boy was five and left the legacy of a good library. Pell was a bright and multi-lingual student and very handsome. Famed in philosophy and math, he established a school in Sussex.
Married in 1632, he had eight kids. Also unpractical and improvident, he struggled with his financial position and died in poverty 12/12/1685.
- Relationship : Marriage 1632 (Lasting)
- Death, Cause unspecified 12 December 1685 (Age 74)
chart Placidus Equal_H.
- Death of Father 1617 (When he was five)
Martin Harvey in Nativitas I, "His youngest uncle guessed 'about noon.'"
- Notable : Famous : Top 5% of Profession
- Notable : Extraordinary Talents : For Languages
- Vocation : Science : Mathematics/ Statistics (Mathematician)
- Family : Parenting : Kids more than 3 (Eight)
- Family : Relationship : Number of Marriages (One)
- Family : Childhood : Family traumatic event (Age five when dad died)
- Lifestyle : Finan


**A — Q8 GGUF** (12.7s, len 1126)


> |تولد فـ | 11 مارس 1611 مع الـ 12 ديال النهار
|المكان | ساوثويك، إنجلترا، 50° 37' 40" N, 0° 13' 40" W
|المنطقة الزمنية | LMT m0w13 (توقيت منتصف الليل المحلي)
|معطيات الفلك | 20° 32' 15° 14' asc. 17° 30'
عالم بريطاني، كان عبقري فـ اللغات والرياضيات. كان ولد واحد القسيس اللي ما كانش متبع التقاليد ومات ملي كان الدري صغير، وخلا لينا مكتبة زوينة. بيل كان تلميذ ذكي وبزاف ديال اللغات، وكان كيبان غزال بزاف. اشتهر بالفلسفة والرياضيات، وأسس مدرسة فـ ساسكس.
تزوج فـ 1632، وولدوا ثمانية ديال الدراري. وحتى هو ماكانش عملي وماكيهتمش بفلوسو، وعاش محارب مع وضعيتو المالية ومات فقير فـ 12/12/1685.
- العلاقة: الزواج 1632 (باقي مستمر)
- الوفاة: السبب غير محدد، مات فـ 12 دجنبر 1685 (فعمرو 74 عام)
البلاصة: Placidus Equal_H
- وفاة الأب: 1617 (ملي كان عندو 5 سنين)
Martin Harvey in Nativitas I, "His youngest uncle guessed 'about noon.'"
- الميزة: مشهور: فـ الـ 5% اللولة ديال المهنة
- الميزة: مهارات استثنائية: لغات
- المهنة: العلم: الرياضيات/ الإحصاء (عالم رياضيات)
- العائلة: التربية: كتر من 3 د الدراري (8)
- العائلة: العلاقة: عدد الزواج (واحد)
- العائلة: الطفولة: حادثة مؤلمة فـ العائلة (ملي كان عندو 5 سنين ومات باه)
- نمط الحياة: المال


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 6. `finephrase/faq` — en len 1200

**English:**

> |Quercus ilex L.|
The Holm oak (also called Holly oak, Evergreen oak) is a large evergreen oak tree native to the Mediterranean region. Its botanic name is Quercus ilex L. The name ilex was the original (Roman) Latin name for this tree, but botanists have now used it for the holly genus.
A typical tree has ridged bark and leathery leaves. The leaves fall in June in the northern hemisphere, after new leaves emerge. Leaves are usually dark green above, and greyish and downy below. The leaf shape is variable on any tree, but those borne on lower branches and on young trees are often toothed or somewhat spiny. This is presumed to be for protection from grazing animals. In this, the foliage resembles that of the common European holly, (Ilex aquifolium), and this resemblance has led to its common and botanic names.
While it is can be grown in much of maritime northern Europe, it is not tolerant of cold continental winters. It can be clipped to form a tall hedge, and it is suitable for coastal windbreaks, in any well drained soil. It forms a picturesque rounded head, with pendulous low-hanging branches. Its size and solid evergreen character gives it a imposing architectural presence that


**A — Q8 GGUF** (10.3s, len 1257)


> |Quercus ilex L.|
شجر البلوط الفليني (اللي كيتسمى حتى شجر الشوك أو شجر الخضور) هو شجر بلوط دائم الخضورية وكبير، أصله من منطقة البحر الأبيض المتوسط. السمية العلمية ديالو هي Quercus ilex L. والسمية "ilex" كانت هي السمية اللاتينية القديمة (الرومانية) ديال هاد الشجر، ولكن دابا العلماء ولاو كيستعملوها باش يهضرو على جنس الشوك.

الشجرة العادية كتكون القشرة ديالها منفوخة والأوراق ديالها رطبة. هاد الأوراق كيطيحوا فشهر يونيو في نص الكرة الشمالية، مورا ما كيخرج الورق الجديد. اللون ديالهم غالباً كيكون أخضر غامق الفوق، وبيض ومبرم من التحت. الشكل ديال الورقة كيختلف على حساب الشجرة، ولكن اللي كيكونوا في الأغصان التحتانية والشجر الصغير غالباً كيكون فيهم سنينات ولا شوك خفيف. هادشي كيتعتبر وسيلة باش يحمي راسو من الحيوانات اللي كتكل الغلة. وفهاد النقطة، الوراق كيشبهوا بزاف لشجر الشوك الأوروبي المعروف (Ilex aquifolium)، وهاد التشابه هو اللي خلا السمية العلمية ديالو تكون معروفة ومشهورة.

وخا يمكن ليه يتزرع فبزاف ديال المناطق الشمالية الأوروبية الساحلية، ولكن ما كيصبرش للبرد القاصح ديال الشتا في المناطق الداخلية. وكيقدر الواحد يقصو باش يدير منو سياج عالي، وهو مناسب بزاف باش يحبس الرياح اللي جاية من البحر، بشرط تكون الأرض فيها تصريف مزيان للماء. كيعطي شكل زوين ومربع، مع أغصان طايحة لتحت. الحجم ديالو والشكل الدائم الخضورية ديالو كيخليه يبان بحال شي عمارة مهيبة.


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 7. `fineweb-edu` — en len 1200

**English:**

> From the time of Aristotle (384-322 BC) until the late 1500’s, gravity was believed to act differently on different objects.
- Drop a metal bar and a feather at the same time… which one hits the ground first?
- Obviously, common sense will tell you that the bar will hit first, while the feather slowly flutters to the ground.
- In Aristotle’s view, this was because the bar was being pulled harder (and faster) by gravity because of its physical properties.
- Because everyone sees this when they drop different objects, it wasn’t questioned for almost 2000 years.
Galileo Galilei was the first major scientist to refute (prove wrong) Aristotle’s theories.
- In his famous (at least to Physicists!) experiment, Galileo went to the top of the leaning tower of Pisa and dropped a wooden ball and a lead ball, both the same size, but different masses.
- They both hit the ground at the same time, even though Aristotle would say that the heavier metal ball should hit first.
- Galileo had shown that the different rates at which some objects fall is due to air resistance, a type of friction.
- Get rid of friction (air resistance) and all objects will fall at the same rate.
- Galileo said that the ac


**A — Q8 GGUF** (9.4s, len 1009)


> من وقت أرسطو (384-322 قبل الميلاد) حتى لخر القرن 15، كان كيظن أن الجاذبية كتعامل بشكل مختلف مع الأشياء.
- طيح مسمار وريش في نفس الوقت... شكون فيهم اللي غادي يوصل للأرض أول واحد؟
- أكيد، العقل الغريزي كيقول ليك المسمار هو الأول، والريش كيطير بشوية حتى يوصل للأرض.
- أرسطو كان كيظن أن هادشي حيت المسمار "كيجر" (كتجرو الجاذبية) كتر وبسرعة بسباب الخصائص ديالو المادية.
- وبما أن كل واحد كيشوف هادشي فاش كيخوي شي حاجة من الفوق، ما تسولاتش لمدة تقريبا 2000 عام.
غاليليو غاليلي كان أول عالم كبير اللي نفى (أثبت غلط) النظريات ديال أرسطو.
- في التجربة المشهورة ديالو (على الأقل بالنسبة لفيزيائيين!)، طلع غاليليو لبرج بيزا المائل وطاح كورة خشبية وكورة رصاصية، بجوجهم نفس الحجم ولكن كتلة مختلفة.
- بجوجهم طاحوا فـنفس الوقت، واخا أرسطو كان كيقول بلي الكورة ديال الحديد اللي ثقيلة هي اللي غادي توصل للأرض أول وحدة.
- غاليليو بين أن السرعة المختلفة باش كيطيحوا بعض الأشياء هي بسبب مقاومة الهواء، وهي نوع من الاحتكاك.
- حيد المقاومة (مقاومة الهواء) وغادي يطيحوا كاع الأجسام بنفس السرعة.
- غاليليو قال بلي الجاذبية هي "القوة"


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 8. `finephrase/faq` — en len 1200

**English:**

> : impressions of land, sea and sky
National Gallery of Australia, Canberra
John Constable (1776–1837) was one of the greatest British landscape painters, renowned for his landscapes, his ‘pure and unaffected representation of nature’. The breadth of his vision of the English countryside is evident in masterpieces such as Salisbury Cathedral from the Bishop’s Grounds 1822–23, A boat passing a lock 1826 and The Vale of Dedham 1827–28.
Constable’s works – from the smallest pencil notation or oil sketch to the finished painting – reveal his deep understanding of nature. His rapid, on-the-spot sketches, such as Rainstorm over the sea c.1824–28, brought a new vivacity to the observation of nature, capturing the effervescent sensations of light and atmosphere. His art became the benchmark for naturalist painters in Europe in the nineteenth century. French artists were the first to be stimulated by the innovative character of his work, which brought a new boldness to the handling of paint.
Constable was a great innovator, but he also had a passionate interest in the works of the Old Masters, and in particular the great tradition of landscape painting of Claude and Ruisdael. He continued to


**A — Q8 GGUF** (9.4s, len 1076)


> لوحات ديال الطبيعة، البحر والسما
المتحف الوطني لأستراليا، كانبرا
جون كونسطابل (1726-1837) كان واحد من أعظم الرسامين البريطانيين اللي كيرسمو المناظر الطبيعية، وكان معروف باللوحات ديالو، وبالطريقة النقية والواقعية باش كيرسم الطبيعة. التنوع الكبير في النظرة ديالو للريف الإنجليزي بان فـلوحات مشهورة بحال "كاتدرائية سالزبوري" من حدائق البطرير (1822-1823)، و"قارب كيدوز فوق قنطرة" (1826) و"واد ديميد" (1827-1828).
لوحات كونسطابل، من أصغر الملاحظات بالرصاص أو التشكيلات الأولية حتى للوحة النهائية، كتبين الفهم العميق ديالو للطبيعة. الملاحظات السريعة والسريعة ديالو في الموقع، بحال "عواصف المطر فوق البحر" (تقريباً 1824-1828)، عطاو واحد الحيوية جديدة للملاحظة ديال الطبيعة، وقدر يلتقط الإحساس القوي بالضوء والجو. الفن ديالو ولا هو المعيار بالنسبة للفنانين الواقعيين في أوروبا فالقرن تسعطاش. الفنانين الفرنسيين كانوا هما أول من تأثروا بالطابع المبتكر ديال الخدمة ديالو، اللي جاب واحد الجرأة جديدة في التعامل مع الصباغة.
كونستابل كان مبتكر كبير، ولكن كانت عندو حتى اهتمام كبير بأعمال الرسامين القدام، وبالخصوص التقليد العظيم ديال لوحات المناظر الطبيعية عند كلود ورويسدايل. وبقى خدام...


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 9. `finephrase/faq` — en len 1200

**English:**

> WebMD Medical News
Louise Chang, MD
Dec. 12, 2007 -- European scientists today announced that it may be possible to treat Duchenne muscular dystrophy by tweaking stem cells from patients' muscles.
In Duchenne muscular dystrophy, muscles weaken over time.
Duchenne muscular dystrophy is one of the nine major types of muscular dystrophy. It's one of the most common forms of muscular dystrophy and only affects boys.
European scientists report that their stem cell technique improved muscle function in mice with Duchenne muscular dystrophy.
The technique "represents a promising approach for Duchenne muscular dystrophy" but needs more work, write the researchers. They included Rachid Benchaouir of Italy's University of Milan.
Here's how their technique works.
First, the researchers took a sample of adult stem cells from the muscles of mice with Duchenne muscular dystrophy.
Next, the scientists tweaked those stem cells to correct the genetic glitch that limits the production of dystrophin, a protein needed for muscle development.
Finally, the researchers injected those stem cells back into the mice.
Within 45 days of stem cell treatment, the tweaked stem cells made more dystrophin, which i


**A — Q8 GGUF** (8.9s, len 1058)


> أخبار الطب من موقع WebMD
لويس تشانغ، دكتور
12 دجنبر 2007 -- علماء أوروبيين أعلنوا اليوم بلي يقدر يكون ممكن علاج مرض "دوشين" (Duchenne muscular dystrophy) عن طريق تعديل خلايا جذعية مستمدة من عضلات المرضى.
فمرض "دوشين"، العضلات كيضعافو مع الوقت.
هذا المرض هو واحد من تسعة ديال الأنواع الرئيسية ديال أمراض العضلات. وهو من أكثر الأمراض اللي كتقيس العضلات، وكياثر غير على الدراري الصغار.
العلماء الأوروبيين قالو بلي الطريقة ديالهم باستعمال الخلايا الجذعية حسنات وظيفة العضلات عند فيران فيهم هاد المرض.
هاد الطريقة "كتمثل نهج واعد بالنسبة لمرض دوشين"، ولكن مازال خاصها دراسات كتر، على حساب الباحثين اللي من جامعة ميلانو في إيطاليا.
ها كيفاش خدامة الطريقة ديالهم:
أول حاجة، العلماء خداو عينة من خلايا جذعية ناضجة من العضلات ديال فيران فيهم مرض دوشين.
من بعد، عدلوا هاد الخلايا الجذعية باش يصححو خلل وراثي كيحد من إنتاج بروتين "الدوفينين" (dystrophin)، اللي هو ضروري باش العضلة تكبر وتطور بشكل عادي.
وفي الأخير، العلماء عاودوا حقنوا دوك الخلايا الجذعية في الفيران.
من بعد 45 يوم من العلاج بالخلايا الجذعية، هاد الخلايا المعدلة بدات تنتج كمية كتر من بروتين الدوفينين.


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 10. `fineweb-edu` — en len 1200

**English:**

> Coffee is an important crop and is crucial to the economy of many developing countries, generating around US$70 billion per year. There are 115 species in the Coffea genus, but only two, C. arabica and C. canephora, are commercially cultivated. Coffee plants are attacked by many pathogens and insect-pests, which affect not only the production of coffee but also its grain quality, reducing the commercial value of the product. The main insect-pest, the coffee berry borer (Hypotheneumus hampei), is responsible for worldwide annual losses of around US$500 million. The coffee berry borer exclusively damages the coffee berries, and it is mainly controlled by organochlorine insecticides that are both toxic and carcinogenic. Unfortunately, natural resistance in the genus Coffea to H. hampei has not been documented. To overcome these problems, biotechnological strategies can be used to introduce an α-amylase inhibitor gene (α-AI1), which confers resistance against the coffee berry borer insect-pest, into C. arabica plants.
We transformed C. arabica with the α-amylase inhibitor-1 gene (α-AI1) from the common bean, Phaseolus vulgaris, under control of the seed-specific phytohemagglutinin prom


**A — Q8 GGUF** (7.7s, len 841)


> القهوة هي واحد المحصول مهم وضروري للاقتصاد ديال بزاف ديال الدول النامية، وكيعطي تقريبا 70 مليار دولار فالعام. كاينين 155 نوع فجنس Coffea، ولكن غير جوج هما C. arabica وC. canephora اللي كيغرسوهم الناس بصفة تجارية. نباتات القهوة كتعرض لعدة أمراض وحشرات مضرة، واللي كأثروا ماشي غير على إنتاج القهوة ولكن حتى على جودة الحبوب ديالها، وكينقصو من القيمة التجارية ديال المنتوج. أهم حشرة مضرة هي "تقب حبوب القهوة" (Hypotheneumus hampei)، واللي كتجيب خسائر سنوية عالمية كتوصل لـ 500 مليون دولار تقريبا. هاد الحشرة كتضرر غير حبوب القهوة، وكتسيطر عليها غالبا بمبيدات حشرات من نوع "البيوتكنولوجيا"، اللي كيكونوا سامين وكيسببو السرطان. للأسف، ما كايناش مقاومة طبيعية فجنس Coffea ضد H. hampei موثقة. وباش نلقاو حل لهاد المشاكل، تقدر تستعمل تقنيات بيولوجية باش تدخل جين ديال مثبط إنزيم الأميلوز (α-AI1) اللي كيعطي مقاومة للحشرة H. hampei فنباتات C. arabica.


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 11. `finephrase/tutorial` — en len 1200

**English:**

> This picture may remind you of an alien landscape, but it's actually a look at our own planet from hundreds of miles above. NASA's Terra satellite captured this view of a 35-mile-wide (55-kilometer-wide) alluvial fan in China's Xinjiang Province in 2002.
The geological feature spreads across the desolate landscape between the Kunlun and Altun mountain ranges that form the southern border of the Taklimakan Desert. Terra's color-coded view shows water flowing down from the mountains along the left side of the fan. Vegetation appears in shades of red in the upper left corner. NASA says the lumpy-looking terrain at the top of the image consists of sand dunes at the edge of the Taklimakan, one of the largest sandy deserts on Earth.
This is one of the first images you'll see in "Earth as Art," a newly published 158-page book featuring satellite pictures of planet Earth. NASA is making the book freely available online in PDF format, but it can also be downloaded as an iPad app or purchased as a coffee-table book from the U.S. Government Printing Office's online store.
"Earth as Art" serves as a great kickoff for this year's Cosmic Log Space Advent Calendar, which highlights views of Earth


**A — Q8 GGUF** (9.6s, len 1113)


> هاد التصويرة تقدر تفكرنا بشي منظر فضائي، ولكن راه غير صورة ديال كوكب الأرض من فوق مئات الكيلومترات. القمر الصناعي Terra ديال ناسا صور هاد المنظر ديال واحد "الفان" (fan) ديال الرملة اللي عرضها 35 ميلي (أو 55 كيلومتر) فإقليم شينجوان فالصين عام 2002.
هاد الميزة الجيولوجية منتشرة فوق الطبيعة القاحلة بين سلاسل جبال كونلون وألتون اللي كتشكل الحدود الجنوبية ديال صحراء تاكلمكان. التصويرة اللي ملونة (بألوان) كتبين الماء اللي هابط من الجبل على جهة اليسار ديال الفان. والنباتات كتبان بألوان حمراء فلقنت العلوي لليسرى. ناسا قالت بلي هاد التضاريس اللي كيبانو بحال شي نقط فوق التصويرة، عبارة عن كثبان رملية فحدود صحراء تاكلمكان، واللي هي وحدة من أكبر الصحاري الرملية فالعالم.
هذا واحد من أول التصاور اللي غادي تشوفهم فكتاب "الأرض كفن" (Earth as Art)، اللي يلاه خرج مؤخراً وفيه 158 صفحة فيها صور ديال القمر الصناعي للأرض. ناسا حطات الكتاب إلكترونياً وبشكل مجاني بصيغة PDF، ولكن ممكن حتى تيليشارجي تطبيق ديالو فـ iPad أو تشريه ككتاب فوق الطبلة من المتجر الإلكتروني ديال المطبعة الحكومية الأمريكية.
"الأرض كفن" هو بداية زوينة لهاد العام ديال "مذكرات الفضاء" (Cosmic Log Space Advent Calendar)، اللي كتركز على صور مختلفة للأرض.


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 12. `finephrase/tutorial` — en len 1200

**English:**

> Catching the Rain: a Great Lakes Resource Guide to Natural Stormwater Management
- Read the full report (PDF)
Most people would agree that getting caught outside during a rainstorm is an unpleasant experience. In addition to getting soaked by the rain, one has to negotiate the many puddles and running water, or "stormwater runoff" that form during a storm. Most people would also agree that, despite the occasional discomforts, rain is an essential part of life, providing fresh water for our rivers, plants, lakes, and ultimately for us to drink. Whether we get water from a well, a river, or other sources, rainwater is critical for replenishing our drinking water supplies.
Unfortunately, stormwater runoff from developed areas is a significant cause of water pollution in the United States. Storm water runoff from residential, commercial, and industrial areas is responsible for 21 percent of the United State's impaired lakes and 45 percent of its impaired estuaries. This is the second highest source of water pollution after agricultural runoff. The question for citizens, developers and municipalities today is how to best manage stormwater runoff. When managed properly, water is a valuab


**A — Q8 GGUF** (8.5s, len 1034)


> كيفاش نتعاملو مع الشتا: دليل للموارد الطبيعية ديال بحيرات كبيرة باش نسيرو مياه الأمطار
- قرا التقرير كامل (PDF)
أغلب الناس كيتفقوا بلي أنك تخرج للبرد فوسط الشتا تجربة خايبة. من غير أنك تعيا بالشتا، خاصك حتى تقابل مع البرك المائية الكبيرة والمجاري المائية اللي كتكونيّر (أو "الروينة" ديال الشتا) ملي كتكون العاصفة. وأغلب الناس كيتفقوا بلي رغم هادشي كامل، الشتا جزء أساسي من الحياة، حيت كتعطينا ما صالح للشرب للوديان، النباتات، والبحيرات، وفي الأخير كنشربوها حنا. سواء كان الما جاي من البير أو الواد أو مصادر أخرى، مياه الأمطار ضرورية باش تعوض الما اللي عندنا ديال الشرب.
للأسف، الروينة المائية (stormwater runoff) اللي كتجي من المناطق العمرانية هي سبب رئيسي للتلوث فالميريكان. هاد الروينة المائية من الأحياء السكنية، التجارية والصناعية هي اللي مسؤولة على 21% من البحيرات الملوثة في الولايات المتحدة و 45% من مصبات الوديان الملوثة. وهذا هو ثاني أكبر مصدر ديال التلوث بعد مياه الفلاحة. السؤال اللي قدام المواطنين والمطورين والمحليات اليوم هو كيفاش نسيرو هاد الروينة المائية بطريقة أحسن. فاش كتكون مديورة ليها الإدارة، الما كيولي مورد قيم


**B — bf16 full precision** (2.7s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00


## 13. `finephrase/faq` — en len 1200

**English:**

> Vanilla is a product of Lussumo:Documentation and Support.
101 to 120 of 223
They say the gesture allows a bug named Cytomegalovirus, which is dangerous in pregnancy, to be passed from man to woman to give her time to build up protection against it.The bug is found in saliva and normally causes no problems. But it can be extremely dangerous if caught while pregnant and can kill unborn babies or cause birth defects.Writing in the journal Medical Hypotheses, researcher Dr Colin Hendrie from the University of Leeds, said: "Female inoculation with a specific male's cytomegalovirus is most efficiently achieved through mouth-to-mouth contact and saliva exchange, particularly where the flow of saliva is from the male to the typically shorter female."
The experiments showed this fecal armor could successfully repel predators. Often the predators did not even investigate the potential meals, presumably because the beetles fooled them into believing they were just turds.
A group of University of Kansas researchers working with Chinese colleagues have discovered a venomous, birdlike raptor that thrived some 128 million years ago in China. This is the first report of venom in the lineage that 


**A — Q8 GGUF** (9.4s, len 1025)


> الفانيلا هي منتوج ديال لوسومو: التوثيق والدعم.
من 101 حتى لـ 120 من 223
كيقولوا بلي هاد الحركة كتخلي فيروس "سيطميغلوڤيروس" اللي كيكون خطير فالحمل، يتنقل من الراجل للمرأة باش يعطيها الوقت باش تبني حماية ضدو. هاد الفيروس كاين فاللعاب وعادي ما كيدير حتى مشكل. ولكن يقدر يكون خطر بزاف إلا تنقل فاش تكون المرأة حاملة ويقدر يقتل المواليد أو يسبب عيوب خلقية. باحث سميتو دكتور كولن هيندري من جامعة ليدز كتب فالمجلة ديال "مليتاليتي هيبوتيسيز" (Medical Hypotheses): "التلقيح عند الأنثى بفيروس سيطميغلوڤيروس خاص الراجل يكون هو اللي كيتلقح، وهادشي كيكون فعال بزاف عن طريق التواصل المباشر بين الفم واللعاب، وبالخصوص فاش كيكون تدفق اللعاب من الراجل للأنثى اللي غالباً كتكون قصيرة." التجارب بينات بلي هاد "الدرع" ديال الميكروبات يقدر يحمي الطيور الجارحة. وغالباً ما كانتش الطريفات كيقلبوا على الماكلة، ربما حيت الحشرات خداتهم فخ وماتيقوش بلي راهم غير فضلات. مجموعة من الباحثين فجامعة كانساس اللي خدامين مع زملاء صينيين لقاو واحد الرابور طير سام كيشبه للثعالب، وعاش هادي تقريبا 128 مليون سنة فالصين. وهادي أول مرة كيكون فيها سم فهاد السلالة.


**B — bf16 full precision** (2.8s, len 0)


> You have exceeded your free GPU quota (60s requested vs. 0s left). Try again in 0:00:00
