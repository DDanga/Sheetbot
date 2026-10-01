# Sheetbot — how it works, conversation by conversation

This is the stage 0 design: written and approved before any code, then kept in sync with
what was actually built. Every example is the bot's real answer on the demo spreadsheet
[`template/sheetbot-demo.xlsx`](../template/sheetbot-demo.xlsx), a made-up corner shop with sales
from July to October 2026, as seen on 1 October 2026.

---

## Three kinds of rules

| Kind | What it does | When it speaks | Example |
|---|---|---|---|
| 🔔 **Notification** | Checks every row against a condition | As soon as the condition is met | Stock < Minimum → "⚠ Milk: 3 left" |
| 📊 **Report** | Totals for a period | On a schedule | Every day at 9:00: yesterday's sales |
| 📈 **Analysis** | Compares periods, ranks products | On a schedule, only when there's something to say | Weekly sales fell by more than 20% |

Rules always use **column names**, never cell addresses. If someone inserts a column or
reorders them, the rules keep working.

---

## 1. First start

```
You: /start

Bot: Hi! I'm watching the spreadsheet “Shop — demo”.
     Sheets: Stock (15 rows), Sales (882), Clients (10).

     You're the first one here, so you're the admin.

     What I can do:
     /newrule — create a notification, report or analysis
     /rules   — all rules
     /report  — send a report now
     /status  — is everything working
```

```
You: /status

Bot: ✅ Spreadsheet “Shop — demo” is reachable

     • Stock — 15 rows, columns: Product, Category, In stock, …
     • Sales — 882 rows, columns: Date, Product, Quantity, …
     🔔 Rules: 3 active, 1 paused
     🔄 Last check: 2 min ago · every 5 min

     🕐 Time zone: Asia/Bangkok
```

---

## 2. Notification: low stock

```
You: /newrule
Bot: What are we creating?
     [🔔 Notification] [📊 Report] [📈 Analysis]
→ 🔔 Notification

Bot: Which sheet?
     [Stock] [Sales] [Clients]
→ Stock

Bot: Which column do we check?
     [Product] [Category] [In stock] [Minimum] [Price] [Purchase price] [Supplier]
→ In stock

Bot: Condition?
     [<] [≤] [=] [≠] [≥] [>] [changed] [empty] [contains]
→ <

Bot: Compare with what?
     [A number] [Another column]
→ Another column
Bot: [Product] [Category] [Minimum] [Price] [Purchase price] [Supplier]
→ Minimum

Bot: Who gets the notification?
     [Me] [Everyone with access] [👥 Stock team]
→ Me

Bot: Let's check:
     🔔 Stock: In stock < Minimum
     👤 Me

     Message:
     ⚠ Whole milk: In stock 3 (Minimum 10)

     Right now it would fire for 4 rows: Whole milk, Sourdough bread, Basmati rice, Oranges.

     [✅ Save]
     [✏ Condition] [✏ Message]
     [✏ Row name] [✏ Recipients]
     [✖ Cancel]
→ ✅ Save

Bot: Saved ✅  🔔 Stock: In stock < Minimum
```

**What comes after that:**

```
Bot: 🔔 Stock: In stock < Minimum

     ⚠ Whole milk: In stock 3 (Minimum 10)
     ⚠ Sourdough bread: In stock 2 (Minimum 12)
     ⚠ Basmati rice: In stock 4 (Minimum 8)
     ⚠ Oranges: In stock 5 (Minimum 12)
```

- **The first check** after saving sends one message with every matching row.
- **No repeats.** While milk stays below its minimum, the bot doesn't mention it again.
- **When it's fixed:** "✅ Whole milk — back to normal". This can be turned off in the rule
  with the "🔁 “Back to normal” messages" button.
- **If the row drops below the minimum again**, a new notification comes.

**The row name** ("Milk" rather than "row 2") is picked automatically: the first column that
holds text, not numbers or dates. If the guess is wrong, "✏ Row name" changes it.

**"✏ Message"** lets you write your own template with any columns in it, for example:
`Order {Product} from {Supplier}, {In stock} left`.

---

## 3. Report: yesterday's sales

```
You: /newrule → 📊 Report

Bot: Which sheet?     → Sales
     (the bot finds the date column itself; if there are several, it asks)
Bot: For which period?
     [Yesterday] [Today] [Last 7 days] [This month] [Last month]
→ Yesterday

Bot: What to count?
     [Sum] [Average] [Maximum] [Minimum] [Number of rows]
→ Sum → of which column? [Quantity] [Amount] [Cost] → Amount
Bot: Counting: Sum of Amount. Add another figure?  [➕ One more] [Done]
→ ➕ → Number of rows → Done

Bot: Split by a column?  [No split] [Product] [Manager]
→ Manager

Bot: When to send it?
     [Every day] [Weekdays] [Once a week] [On the 1st of the month]
→ Every day
Bot: At what time?  [08:00] [09:00] [10:00] [18:00] [Other time]
→ 09:00

Bot: Who gets it?  → Me

Bot: Preview: this is what the report would look like if it came now.
     [✅ Save] [✏ Figures] [✏ Period] [✏ Schedule] [✏ Recipients]
```

**What arrives every day at 9:00:**

```
Bot: 📊 Sales · 30.09

     Amount: $42.82 · Rows: 5

     By Manager:
     Anna — $24.42 · 3
     Ben — $13.00 · 1
     Chloe — $5.40 · 1
```

Money keeps the currency the sheet shows: `$1,827.32` in English, `1 827,32 ₽` in Russian.

`/report` sends any of the saved reports right now: the bot asks which one.

---

## 4. Analysis: a drop in weekly sales

```
You: /newrule → 📈 Analysis

Bot: What do we analyse?
     [Compare periods] [Top items]
→ Compare periods

Bot: Sheet → Sales · Date column → Date
Bot: Which periods?       [Day to day] [Week to week] [Month to month]
→ Week to week
Bot: What to count?       [Sum] [Average] [Maximum] [Minimum] [Number of rows]
→ Sum → Amount

Bot: When to tell you?
     [Always] [If it fell by more than…] [If it changed by more than…]
→ If it fell by more than… → [10%] [20%] [30%] [Other]
→ 20%

Bot: When to check? → Once a week, Monday, 09:00
Bot: Who gets it? → Me
Bot: [✅ Save]
```

**What arrives when the condition is met:**

```
Bot: 📉 Amount: −32.7% · week to week

     24.09–30.09: $303.78 (rows: 34)
     17.09–23.09: $451.08 (rows: 56)

     Leaders: Sourdough bread $55.00 · Coffee beans $44.70 · Baguette $36.40
```

If sales didn't fall by more than 20%, the bot stays quiet on Monday. With "Always" and month to
month, the same analysis shows the recovery after the August slump:

```
Bot: 📈 Amount: +20.6% · month to month

     01.09–30.09: $1,827.32 (rows: 211)
     01.08–31.08: $1,515.12 (rows: 180)

     Leaders: Coffee beans $223.50 · Sourdough bread $220.00 · Whole milk $197.40
```

---

## 4b. Analysis: top products and "hidden stars"

Shows not only what sells best, but also what earns the most. Profit needs a cost column
(in the demo spreadsheet it's «Cost» on the «Sales» sheet).

```
You: /newrule → 📈 Analysis → Top items

Bot: Sheet → Sales · Date column → Date
Bot: What counts as an item?   [Product] [Manager]
→ Product
Bot: Quantity column?          [Quantity] [Number of rows]
→ Quantity
Bot: How to count profit?
     [Revenue − cost] [There's a profit column] [Without profit]
→ Revenue − cost → Amount → Cost
Bot: For which period?         [Last 7 days] [This month] [Last month]  → Last month
Bot: How many places?          [3] [5] [10]  → 3
Bot: When to send it?          → On the 1st of the month, 10:00
Bot: [✅ Save]
```

**What arrives:**

```
Bot: 🏆 Top · 01.09–30.09

     By quantity:
     1. Greek yogurt — 80
     2. Sparkling water — 78
     3. Whole milk — 60

     By profit:
     1. Coffee beans — $121.50
     2. Sourdough bread — $92.00
     3. Croissant — $70.00

     💎 Hidden star: Coffee beans
     Only #13 by quantity (15), but #1 by profit, margin 54%.
     Greek yogurt sells best but made only $63.20.
```

A "hidden star" is a product that ranks much higher by profit than by sales volume.
The bot finds them by itself and calls them out separately.

---

## 5. The rule list

```
You: /rules

Bot: Rules (3):
     [🔔 Stock: In stock < Minimum]
     [📊 Sales · yesterday · 09:00]
     [⏸ Sales: week to week, −20%]
→ 🔔 Stock: In stock < Minimum

Bot: 🔔 Stock: In stock < Minimum
     👤 Me
     Status: ▶ on
     [✏ Edit] [⏸ Pause] [🧪 Check now] [🗑 Delete]
```

---

## 6. When something breaks

Someone renamed the column «In stock» to «In stock, pcs»:

```
Bot (to the admins):
     ⚠ The rule «Stock: In stock < Minimum» is paused.
     The column «In stock» is gone from the sheet «Stock». Edit the rule.
     Similar name in the sheet: «In stock, pcs».
     [Use «In stock, pcs»] [✏ Edit]
```

If the bot loses access to the spreadsheet, or the Google key stops working, the admins get one
message, and the bot keeps trying until the connection is back.

---

## 7. Access (stage 6)

- **The admin** is the first person to press `/start`. There can be several admins.
- **Inviting someone:** `/invite` gives a one-time link valid for 24 hours. The person opens it,
  and the admin gets "🔑 Anna asks for access" with buttons: pick a team (an existing one or
  "➕ New team") or "✖ Reject".
- **Teams**, for example «Stock» or «Sales». In a rule, report or analysis, the "Who gets it"
  step shows a "👥 Stock" button for each team.
- **Members** receive notifications, reports and analyses, can use `/report` and `/status`, and
  can pause their own notifications. Only admins create and change rules, invite and manage people.
- **`/users`** lists everyone: make or remove an admin, change the team, remove access.
  The last admin can't be demoted or removed.
- **`/pause`** mutes your own notifications: for an hour, until the morning (09:00), or until
  you turn them back on. System messages to admins (a broken rule, no access to the sheet) still
  come through.
- **A stranger** without an invite is told the bot is private, and given their chat ID.

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Greeting; the first user becomes the admin |
| `/newrule` | Create a notification, report or analysis |
| `/rules` | All rules: edit, pause, check, delete |
| `/report` | Send a report right now |
| `/status` | Connection to the sheet, rule count, time of the last check |
| `/pause` | Pause your notifications: 1 h / until morning / until you turn them on |
| `/invite` | Invite a person (admins only) |
| `/users` | People and teams (admins only) |
| `/lang` | Switch between English and Russian |

---

## Decisions (approved)

1. **Language:** English and Russian, switchable; English on first start.
2. **Checking the sheet** for notifications: every 5 minutes, configurable.
3. **"✅ Back to normal"** is sent by default and can be turned off per rule.
4. **A report with no data** is still sent ("No data for this period."), not skipped.
5. **Added on request:** the "Top items" analysis by quantity and by profit, with "hidden stars"
   (section 4b). For it, the demo spreadsheet got the columns «Purchase price» (Stock) and «Cost» (Sales).
6. **The demo spreadsheet is in English**, with four months of sales (July–October), so periods
   and month-to-month comparisons have something to compare.
