-- Editorial clean-up of the published blog (AdSense "low-value content").
--
-- Before this migration six posts were live, five of them 189-234 words of
-- generic copy, three of them the same React/Python/Java article published
-- three times. This migration:
--
--   1. Rewrites the three posts that each serve a distinct reader need, in
--      place, keeping their URLs (slugs) and original published_at:
--        - decoding-the-true-price-tag-of-software-development-in-india
--          (custom CRM cost and budgeting)
--        - what-dictates-the-ecommerce-website-development-cost-in-india
--          (ecommerce store cost drivers)
--        - react-frontend-with-python-and-java-backend-development-for-scaling-businesses
--          (the three overlapping backend articles, consolidated)
--   2. Unpublishes the two merged duplicates. Their URLs already 308-redirect
--      to the consolidated article (frontend vercel.json).
--   3. Unpublishes the social-media post, which had no substance to keep and
--      is outside what this site can write about from experience.
--
-- Nothing is deleted: unpublished rows stay in blog_posts for reference.
-- updated_at is set so the article pages and sitemap show the revision date.
-- Safe to re-run (every statement targets a fixed slug and sets fixed values).
-- Run it BEFORE deploying the frontend that drops the word-count filter.

begin;

-- 1a. Custom CRM cost guide ------------------------------------------------
update blog_posts set
  title = 'Custom CRM Development Cost in India: What Drives the Price and How to Budget',
  excerpt = 'Where the effort in a custom CRM really goes, how to compare a build with a per-seat CRM subscription over three years, and what every quote should contain before you sign it.',
  meta_title = 'Custom CRM Development Cost in India: Budgeting Guide',
  meta_description = 'What drives the cost of a custom CRM in India, how to compare it with a per-seat CRM subscription over three years, and what to check in every quote.',
  keywords = array['custom CRM development cost in India', 'CRM total cost of ownership', 'custom CRM vs packaged CRM', 'software development quote checklist'],
  tags = array['Custom CRM', 'Software Cost', 'India'],
  topic_tag = 'Custom CRM Pricing',
  content = $md$A custom CRM is rarely priced by the screen. It is priced by the rules behind the screens: who can see which records, what happens when a lead changes stage, which systems have to stay in sync, and how much existing data has to move across. Two CRMs that look the same in a demo can differ several times over in effort because one of them has to talk to WhatsApp, Tally and a payment gateway and enforce three levels of approval.

This guide explains where the effort goes, how to compare a build against a per-seat CRM subscription fairly, and what a quote should contain before you sign it. It does not quote market rates. Day rates in India vary widely with team size, seniority and city, and a single number would mislead more than it helps. Instead you get a method you can apply to the quotes you actually receive.

## First decide: build, configure or subscribe?

Before pricing a build, check whether you need one. A packaged CRM such as Zoho CRM, Freshsales, HubSpot or Salesforce, configured well, is the cheaper and faster option for many teams. A custom build starts to make financial sense when one or more of these is true:

- You pay for a higher subscription tier mainly to unlock one or two features, such as custom roles or approval steps.
- Your team keeps side spreadsheets because the CRM's stages and fields cannot represent your process.
- Data is copied by hand between the CRM and your accounting, inventory or delivery systems.
- Customers or partners need their own login to see their records, which per-seat pricing makes expensive.
- Your seat count is growing faster than revenue per seat, so the subscription line keeps climbing.

If none of these apply, spend the budget on configuring an existing tool and training your team to use it.

## The six things that drive build effort

### 1. Record types and how they relate

Leads, contacts, companies, deals, activities, quotes, invoices and documents are each a record type with its own fields, list view, detail view, search and permissions. Effort grows with the number of types and, more sharply, with the relationships between them: a deal linked to several contacts, a quote with line items and versions, a contact who belongs to two companies.

### 2. Permissions

"Sales reps see only their own leads, managers see their team's, finance sees everything but cannot change stages" sounds like one requirement. In practice it touches every list, search, export and API endpoint, and it must be enforced on the server, not just hidden in the interface. Record-level ownership rules usually cost more than simple role checks.

### 3. Workflow and automation rules

Assignment rules, follow-up reminders, stage-based emails, SLA timers and approval chains are where most of the business logic lives. Each rule needs a trigger, conditions, a defined behaviour when something fails, and a record of what it did. Write your rules down as "when X happens, if Y, then Z" sentences before you ask for quotes. The number of sentences predicts effort better than the number of screens.

### 4. Integrations

Every external system adds work in three places: the connection itself, handling its failures and retries, and keeping data consistent when both sides change. Common ones for Indian businesses are the WhatsApp Business Platform, email, telephony or IVR providers, payment gateways such as Razorpay, accounting software such as Tally or Zoho Books, and lead sources such as website forms and marketplace enquiries. A system with a documented API and webhooks is far cheaper to integrate than one that requires parsing exported files or reading a legacy database directly.

### 5. Data migration

Moving years of spreadsheet or old-CRM data is often underestimated. Someone has to map old columns to new fields, decide what happens to duplicates and half-filled rows, and run at least one trial import that your team checks before the real switch-over. Ask each vendor how they will handle it and whether a trial migration is included.

### 6. Reports and dashboards

A few fixed dashboards, such as pipeline value by stage, conversion by source and workload by owner, are modest work when the data model is designed for them. A general report builder, scheduled exports, or reports that combine CRM data with accounting or inventory data add significantly more.

## Compare a build with a subscription over three years

Setting a one-time build price against a monthly subscription is misleading. Compare the total cost of ownership over the same period; three years is a reasonable horizon for business software. Put both options in the same worksheet.

**Subscription CRM, three years**

- Per-user monthly price × number of users × 36, using the tier you actually need, not the entry tier.
- Users you expect to add during the period, including any partner or client logins.
- Paid add-ons, extra storage, API or automation limits, and WhatsApp messaging charges.
- Setup, configuration and training, whether done in-house or by a consultant.
- Integration work to connect it to your other systems. A packaged CRM does not make integrations free.

**Custom CRM, three years**

- The initial build, split into phases (see below).
- Hosting, database, backups, email and messaging charges.
- A maintenance allowance for security updates, dependency upgrades and small fixes.
- An enhancement budget, because a CRM that fits your process will keep being asked to do more.

Laid side by side, the answer is often clear. For a small team with a standard sales process the subscription usually wins. For a larger team, a team paying for a premium tier to get a few features, or a business that needs customers to log in, a build can come out ahead within the period. The worksheet also shows which assumption the decision rests on, usually seat count, so you know what to double-check.

## What a useful quote contains

A single lump sum tells you very little. Ask every vendor for the same breakdown so you can compare like with like:

- **Scope by module**, listing the main screens, rules and integrations, and anything explicitly excluded.
- **Effort or cost per module**, so you can see where the money goes and what could be deferred.
- **Assumptions**: who provides content and test data, how many rounds of design changes are included, and which third-party fees you pay directly.
- **Data migration**: what is migrated, how duplicates are handled, and whether a trial run is included.
- **Testing and acceptance**: how you will sign off each phase.
- **Ownership**: whether you receive the source code, database access and deployment credentials.
- **After launch**: what the warranty period covers and what a maintenance arrangement costs afterwards.

Be careful with a quote that is much lower than the others. The usual reasons are missing permissions work, no data migration, no automated tests, or integrations described as "simple" before anyone has read the provider's documentation.

## Phasing is the most reliable way to control cost

The cheapest CRM is one that ships a usable first phase quickly and then grows from real use. A typical split:

1. **Phase 1:** lead capture from your main channels, a pipeline with your real stages, ownership and basic roles, activity history and one manager dashboard. Migrate active records only.
2. **Phase 2:** the automations your team asks for once they use it every day, quotes or proposals, and the most valuable integration.
3. **Phase 3:** client or partner portals, deeper reporting and the remaining integrations.

Phase 1 should be usable on its own. If a vendor cannot describe a first phase your team could work in, the scope is probably not understood yet.

## Running costs to budget for

Running a custom CRM usually costs much less than building it, but never nothing:

- Cloud hosting and a managed database, which scale with users and data.
- Backups, and a restore procedure that has actually been tested.
- Transactional email and WhatsApp messaging, billed by those providers.
- Security patches and framework upgrades. Libraries and third-party APIs change whether or not you add features.
- Monitoring, so you learn about a failed integration before your sales team does.

## Prepare a brief before you ask for quotes

Send every vendor the same brief and the quotes become comparable:

- A list or diagram of how a lead becomes a customer today, and who touches it at each step.
- The record types you need and the fields that matter in reports.
- Your roles, and what each role may see and change.
- The "when X, if Y, then Z" rules you want automated.
- Each integration, with a link to its API documentation if you have one.
- A sample of the data to be migrated, with its real problems left in.
- The three reports your managers check every week.

The brief is useful even if you decide not to build. It is exactly what you need to configure a packaged CRM properly.

## Frequently asked questions

### How long does a custom CRM take to build?

It depends on scope far more than on the vendor. A focused first phase can be ready in weeks; a CRM with many integrations, a client portal and a large migration takes months. Ask for a timeline per phase, not only a launch date.

### Can we start with a packaged CRM and move to a custom one later?

Yes, and it is often sensible. Using a packaged CRM first shows you which features your team really uses. Check that you can export all of your data, including activity history, so that a later migration is possible.

### Who owns the code?

Your contract decides that, not convention. Agree in writing that you receive the source code, database access and deployment credentials.

If you would like a second opinion on a CRM brief or on quotes you have received, see our [custom CRM development](/custom-crm-development) page or [contact us](/contact) with your process map.$md$
where slug = 'decoding-the-true-price-tag-of-software-development-in-india';

-- 1b. Ecommerce cost guide -------------------------------------------------
update blog_posts set
  title = 'What Drives Ecommerce Website Development Cost in India: A Practical Breakdown',
  excerpt = 'Platform choice, product complexity, payments, shipping, GST invoicing and sale-day traffic: the decisions that move the price of an Indian online store, and the running costs that continue after launch.',
  meta_title = 'Ecommerce Website Development Cost in India: Breakdown',
  meta_description = 'How platform choice, payments, shipping, GST invoicing and catalogue size shape ecommerce website development cost in India, and the running costs to plan for.',
  keywords = array['ecommerce website development cost in India', 'Shopify vs WooCommerce vs custom', 'online store development India', 'ecommerce payment gateway integration'],
  tags = array['Ecommerce', 'Software Cost', 'India'],
  topic_tag = 'Ecommerce Website Development',
  content = $md$Ask three agencies to quote for "an online store" and you can easily get three figures that are far apart. Usually they are not quoting for the same thing. One is pricing a theme on a hosted platform, another a WooCommerce site with a dozen plugins, and the third a custom storefront with its own order system. Each can be the right answer for a different business.

This breakdown covers the decisions that move the price of an Indian ecommerce site, roughly in the order they matter, and the running costs that continue after launch. It deliberately avoids rupee figures: rates vary by team and city, and a number without your scope attached is not useful. Use it to write a clear brief and to read the quotes that come back.

## Decision 1: hosted platform, WooCommerce or a custom build

This choice affects cost more than any single feature.

**Hosted platforms such as Shopify.** You pay a monthly subscription and use the platform's checkout, hosting and security. Development work is mostly theme setup, configuration and apps, and it is the fastest way to launch a standard catalogue store. The trade-offs are recurring app subscriptions, limits on how far checkout and data can be customised, and platform fees. Indian stores usually connect a third-party payment gateway, so check the current plan terms for any transaction fee charged in that case.

**WooCommerce on WordPress.** The plugin is free; you pay for hosting, a theme, paid extensions and the time to keep WordPress, the theme and every plugin up to date. It suits stores that also need a strong content site. The cost that is easy to miss is maintenance: plugin conflicts and security updates need someone responsible for them.

**A custom build**, for example a React storefront with a Python or Java backend and PostgreSQL. It has the highest upfront cost, but no per-order platform fee and full control over checkout, data and integrations. It is justified when the business model does not fit a standard catalogue: made-to-measure products, configurable bundles, per-customer B2B pricing, subscriptions with unusual rules, or tight integration with an existing ERP or warehouse system.

A useful test: write down the five things that make your store different from a typical catalogue store. If a hosted platform plus well-supported apps can handle all five, start there.

## Decision 2: catalogue and product complexity

A few hundred simple products with photos and a price are straightforward on any platform. Effort rises with:

- **Variants and options** such as size, colour and material, especially when stock is tracked per variant.
- **Made-to-order or personalised products**, where the customer enters measurements, uploads a file or picks components, and that data has to travel with the order to production.
- **Catalogue imports** from a supplier feed or an ERP, which need field mapping, validation and a schedule.
- **Search and filtering** across many attributes, which needs proper indexing to stay fast as the catalogue grows.

## Decision 3: payments for Indian buyers

Indian customers expect UPI alongside cards, net banking and wallets. Payment gateways such as Razorpay, PayU, Cashfree and CCAvenue offer these through one integration. The integration is well documented; what separates a reliable store from a fragile one is the work around it:

- Confirming payment status on the server through the gateway's webhook or status API, never trusting the browser redirect alone.
- Handling a payment that succeeds after the customer has closed the tab, so the order is still confirmed.
- Making webhook handling idempotent, so a retried notification cannot confirm or refund an order twice.
- Full and partial refunds from the admin panel.

Gateway charges are per transaction and set by each gateway. Compare their current pricing pages rather than relying on anyone's summary.

**Cash on delivery** is still expected in many categories, and it adds work: limiting COD by pincode or order value, confirming COD orders before dispatch, and tracking parcels returned to origin when a customer refuses delivery. The cost of refused parcels belongs in your margin calculations, not only your development budget.

## Decision 4: shipping and fulfilment

Shipping aggregators such as Shiprocket and couriers such as Delhivery provide APIs for pincode serviceability, rate calculation, label generation and tracking. A basic integration creates shipments and shows tracking links. A deeper one checks serviceability at checkout, chooses a courier by rules, handles returns and syncs delivery status back to the order automatically. Decide which of these you need on day one; the deeper version can follow once order volume justifies it.

## Decision 5: GST invoicing and accounting

Your store needs tax invoices that show your GSTIN, the HSN code for each item and the correct tax split, CGST and SGST for sales within your state and IGST for sales to other states, plus credit notes for returns. Hosted platforms usually handle this through apps; a custom build has to implement it. If your accountant works in Tally, Zoho Books or similar, decide early whether orders flow there automatically or are exported on a schedule. Confirm your registration and invoicing obligations with your chartered accountant; the store should follow their requirements, not the other way round.

## Decision 6: sale-day traffic

Festive sales and influencer campaigns create short, sharp spikes. On a hosted platform the provider handles most of the scaling. On WooCommerce or a custom build, plan for cached catalogue pages, an image CDN, a database sized and indexed for the busiest hour rather than the average one, and a load test before the sale rather than during it. Stock needs particular care: two customers must not both buy the last unit, so reserve stock inside a database transaction at checkout.

## Running costs to plan for

The launch price is only part of the cost. Ask every vendor to list:

- Platform and app subscriptions, or hosting and paid plugin licences.
- Payment gateway charges per transaction.
- Shipping charges and the cost of returns, including refused COD parcels.
- Transactional email and SMS or WhatsApp notifications.
- Maintenance: security updates, platform and plugin updates, and fixes when an integration partner changes its API.
- Small improvements after launch, which every active store needs.

## What to send agencies for comparable quotes

- Your platform preference, or why you are unsure.
- The number of products and variants, and how stock is managed today.
- Payment methods, including whether you need COD and where.
- Your shipping partners or the regions you ship to.
- How invoices and accounts are handled now.
- Existing systems to integrate, such as an ERP, POS or CRM.
- Expected orders on a normal day and on your biggest sale day.
- Three stores you like, and what specifically you like about each.

With the same brief in hand, quotes will differ for real reasons you can discuss, not because each vendor imagined a different store.

## Frequently asked questions

### Should a new brand start with a custom build?

Usually not. A hosted platform lets you test demand quickly. Move to a custom build when the platform's limits are costing you sales or margin and you can name those limits.

### Can we move from Shopify or WooCommerce to a custom store later?

Yes. Plan the migration of products, customers and order history, and permanently redirect old product URLs to the new ones so search traffic is not lost.

### Who is responsible for security on a custom store?

The development team, and it should be written into the contract: server-side payment verification, protected admin access, regular dependency updates and tested backups.

For an example of a custom build, see our [VStitch case study](/case-studies/vstitch-by-anjali-nanda), a made-to-measure fashion store with Razorpay payments and automated PDF invoices, or read about our [web development service](/services/web-development).$md$
where slug = 'what-dictates-the-ecommerce-website-development-cost-in-india';

-- 1c. Consolidated React + Python + Java architecture guide ---------------
update blog_posts set
  title = 'React with a Python and Java Backend: When Two Backend Languages Make Sense',
  excerpt = 'When pairing Spring Boot and FastAPI behind a React frontend is worth the extra cost, how to split responsibilities between them, and a working example of one service calling the other safely.',
  meta_title = 'React, Python and Java Backend: When to Use Both',
  meta_description = 'When to pair a React frontend with both Java and Python services, how to divide the work between Spring Boot and FastAPI, and a working example with fallbacks.',
  keywords = array['React frontend with Python and Java backend', 'Spring Boot and FastAPI together', 'polyglot backend architecture', 'Java and Python microservices'],
  tags = array['React', 'Spring Boot', 'FastAPI', 'Architecture'],
  topic_tag = 'Web Development Tech Stack',
  content = $md$A React frontend backed by both a Java service and a Python service is a common architecture in teams that combine transactional business software with data science. It is also a common source of avoidable cost when it is chosen for the wrong reasons. This guide covers when the combination earns its keep, how to divide responsibilities, and how to connect the pieces safely, with a small working example.

## Start with one backend unless you have a reason for two

Two backend languages mean two build pipelines, two sets of dependencies to patch, two runtimes to monitor, and developers who need to read both. That overhead is worth paying only when each language gives your project something the other cannot provide cheaply.

For most business applications, such as a customer portal, an internal tool, a booking system or a CRM, one backend is enough:

- **Java with Spring Boot** suits large, long-lived codebases with complex transactional rules, several developers working in parallel, and strict typing as a guard rail.
- **Python with FastAPI** suits smaller teams that want to move quickly, and applications whose core is data processing or machine learning.

Both can call a hosted large language model API, so "we want AI features" is not, on its own, a reason to run both.

## When a second backend language pays off

The combination makes sense when the work genuinely divides:

- **Your core business logic already runs on the JVM.** Orders, billing, inventory and permissions live in an existing Spring Boot application, and rewriting them is not an option.
- **You need Python's data and machine-learning ecosystem.** Training and serving models with libraries such as scikit-learn, PyTorch or pandas, and the data scientists who use them, work in Python.
- **The two parts change at different speeds.** A risk or recommendation model is retrained and redeployed often; the order workflow changes rarely and must stay stable.
- **Different teams own them.** A clear service boundary lets each team release without coordinating every change with the other.

If two or more of these are true, a split is worth considering. If none are, keep one backend.

## A sensible division of responsibilities

A pattern that works well in practice:

- **React talks to one backend only**: the Java application, or an API gateway in front of it. The browser should not need to know that a Python service exists. That keeps authentication, CORS and error handling in one place.
- **Spring Boot owns the system of record**: users, permissions, orders, payments and anything that needs database transactions. It validates every request and decides what the user is allowed to do.
- **FastAPI provides focused capabilities behind the Java service**: scoring, recommendations, document processing or forecasting. It receives only the data it needs, returns a result, and does not write to the core database.

The most important rule is **one owner for each piece of data**. If both services write to the same tables, every schema change needs both teams and both languages, and most of the benefit of separating them is lost.

## Worked example: scoring cash-on-delivery orders

Suppose an online store wants to ask for prepayment on cash-on-delivery orders that look likely to be refused at the door. The order flow lives in Spring Boot. The data team has a model, built in Python, that estimates the chance of refusal from an order's details.

### The Python service

The FastAPI service exposes a single endpoint with a typed, validated contract. Amounts are integers in paise to avoid floating-point rounding.

```python
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class ScoreRequest(BaseModel):
    amount: int = Field(gt=0, description="Order total in paise")
    items: int = Field(ge=1)
    pincode: str = Field(pattern=r"^\d{6}$")
    refusals: int = Field(ge=0, description="Earlier COD orders this customer refused")


class ScoreResponse(BaseModel):
    risk: float = Field(ge=0, le=1)
    model: str


def predict_refusal_risk(order: ScoreRequest) -> float:
    # Stand-in for the trained model so the example runs on its own.
    risk = 0.05 + 0.15 * min(order.refusals, 3)
    if order.amount > 500_000:  # above Rs 5,000
        risk += 0.10
    return min(risk, 1.0)


@app.post("/score", response_model=ScoreResponse)
def score(order: ScoreRequest) -> ScoreResponse:
    return ScoreResponse(risk=predict_refusal_risk(order), model="cod-risk-v3")
```

Pydantic rejects malformed input with a 422 response before your code runs, and FastAPI publishes the contract as an OpenAPI document at `/openapi.json` automatically.

### The Java client

The Spring Boot side calls the service with strict timeouts and treats any failure as "no score", never as an error the customer sees.

```java
public record ScoreRequest(long amount, int items, String pincode, int refusals) {}

public record ScoreResponse(double risk, String model) {}

@Component
public class RiskClient {

    private static final Logger log = LoggerFactory.getLogger(RiskClient.class);
    private final RestClient restClient;

    public RiskClient(@Value("${risk.base-url}") String baseUrl) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(1));
        factory.setReadTimeout(Duration.ofSeconds(2));
        this.restClient = RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
    }

    public Optional<ScoreResponse> score(ScoreRequest request) {
        try {
            return Optional.ofNullable(restClient.post()
                    .uri("/score")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(request)
                    .retrieve()
                    .body(ScoreResponse.class));
        } catch (RestClientException e) {
            log.warn("Risk service unavailable; using the fallback rule", e);
            return Optional.empty();
        }
    }
}
```

The order service makes the business decision, with a simple rule to fall back on:

```java
boolean requirePrepayment = riskClient.score(request)
        .map(result -> result.risk() >= 0.4)
        .orElseGet(() -> request.amount() > 1_000_000); // no score: prepay above Rs 10,000
```

Three details matter more than the code itself:

- **The decision stays in Java.** Python returns a number; the order service owns the rule that turns it into "ask for prepayment". Changing the threshold does not require the data team.
- **Failure has a defined behaviour.** If the model is slow or down, checkout still works using a conservative rule. Without timeouts, a stuck Python service would hold Java request threads until checkout itself stopped responding.
- **Store the model version with the order.** When someone later asks why an order needed prepayment, `cod-risk-v3` tells you which model made the call.

The thresholds above are illustrative; a real system would set them from the model's measured accuracy and the business's tolerance for refused parcels.

## Contracts, not shared code

The two services share a contract, not a library. Treat the OpenAPI document as the source of truth: generate or check the Java request and response types against it, and add a contract test to both pipelines so a renamed field fails a build instead of failing in production. Change the contract by adding optional fields first and removing old ones only after both sides have been deployed.

## Security between the services

The user's token is checked once, by the Java application. The Python service should not be reachable from the internet at all; run it on a private network and authenticate calls from the Java service with a service credential or mutual TLS. Send it only the fields the model needs. In the example above that excludes the customer's name, phone number and address.

## Synchronous calls or a message queue?

Use a synchronous HTTP call, as above, when a user is waiting for the answer, and keep it fast and bounded by timeouts. Use a message broker such as RabbitMQ or Kafka for work that can finish later: refreshing recommendations, processing uploaded documents or generating reports. The Java service publishes an event, the Python service consumes it and publishes a result, and neither waits for the other.

## Running two stacks

- Package both services as containers and give each its own build and deployment pipeline.
- Expose health endpoints on both so the platform can restart an unhealthy instance and stop routing traffic to it.
- Pass a correlation ID, such as the W3C `traceparent` header that OpenTelemetry uses, from React through Java to Python, so one customer's request can be followed across all three logs.
- Pin dependency versions in both ecosystems and schedule upgrades for both. Two languages means two sets of security advisories to follow.

## Checklist before you commit

- Can you name the capability that requires the second language?
- Does each table have exactly one owning service?
- Does the browser talk to one backend only?
- Does every cross-service call have a timeout and a fallback?
- Is the contract versioned and tested in both pipelines?
- Do you have people who can debug and maintain both stacks?

If any answer is no, fix that first, or stay with one backend until you need the second.

## Frequently asked questions

### Can React call the FastAPI service directly?

It can, but then authentication, CORS, rate limiting and error handling have to be built and kept consistent in two places. Routing through the Java application or a gateway keeps them in one.

### Should the services use gRPC instead of REST?

gRPC is efficient for high-volume internal calls and gives you a strict schema. REST with JSON is easier to inspect and debug and is enough for most business workloads. Start with REST and move a specific call to gRPC if measurements show it is the bottleneck.

### Can Spring Boot call machine-learning models without Python?

Sometimes. Hosted model APIs can be called from Java, and some trained models can be exported to formats that run on the JVM. If your data team works in Python and retrains often, a Python service is usually simpler to operate.

Further reading on this site: [calling APIs with RestClient](/learn/lessons/lesson_spring_boot_restclient), [an introduction to FastAPI](/learn/lessons/lesson_python_introduction_to_fastapi), [retries, rate limits and circuit breakers in Spring Boot](/learn/lessons/lesson_spring_boot_resilience_retries_rate_limits_and_circuit_breakers) and [connecting React to an API](/learn/lessons/lesson_react_api_integration). If you are weighing this architecture for your own product, our [software development team](/software-development-company-gurugram) can review the plan with you.$md$
where slug = 'react-frontend-with-python-and-java-backend-development-for-scaling-businesses';

-- Every rewritten post gets a fresh revision date and a recomputed word
-- count (same rule as services/blog_service.count_words: runs of non-space).
update blog_posts set
  word_count = array_length(regexp_split_to_array(btrim(content), '\s+'), 1),
  updated_at = now()
where slug in (
  'decoding-the-true-price-tag-of-software-development-in-india',
  'what-dictates-the-ecommerce-website-development-cost-in-india',
  'react-frontend-with-python-and-java-backend-development-for-scaling-businesses'
);

-- 2 + 3. Unpublish the merged duplicates and the social-media post ----------
update blog_posts set
  is_published = false,
  updated_at = now()
where slug in (
  'react-frontend-with-python-and-java-backend-development-for-scaling-businesses-2',
  'react-frontend-with-python-and-java-backend-the-ultimate-tech-stack-for-growing-businesses',
  'stop-chasing-every-social-platform-the-focused-content-strategy-that-actually-drives-revenue'
)
and is_published = true;

commit;
