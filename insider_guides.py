"""
insider_guides.py - Guias escritas de tickerpress.net (/guides/).

Texto fijo y revisado a mano: explica como leer los datos que muestra la web. Lo usa insider_site.build().
Cada guia es (slug, titulo, descripcion, html). Los huecos {example} se rellenan con una compra real
del dia (ver insider_site.guide_example), asi el ejemplo siempre coincide con un Form 4 de verdad.
"""

UPDATED = "November 16, 2026"

FORM4 = ("how-to-read-form-4", "How to Read an SEC Form 4 (Insider Trading Filing)",
         "A plain-English walk through SEC Form 4: who files it, the deadline, each column of Table I, "
         "transaction codes and the footnotes that change what a trade means.", """
<p>Every time a director, an executive officer or a shareholder with more than 10% of a US public company buys or sells
the company's stock, they have to tell the SEC. The form they use is <b>Form 4</b>, and it is public the moment it is
filed on EDGAR. Ticker Press is built entirely from these filings. This guide explains how to read one yourself.</p>

<h2>Who has to file</h2>
<p>Form 4 comes from Section 16 of the Securities Exchange Act of 1934. It applies to three groups, usually called
“insiders” or “Section 16 reporting persons”:</p>
<ul>
<li><b>Directors</b> of the company.</li>
<li><b>Officers</b>: the CEO, CFO, principal accounting officer and other executives with policy-making functions.</li>
<li><b>Beneficial owners of more than 10%</b> of a class of the company's registered equity, which is often an
investment fund rather than a person.</li>
</ul>
<p>A new insider first files a Form 3 listing what they own. After that, every change goes on a Form 4, and a few
exempt transactions can be reported later on the annual Form 5.</p>

<h2>The deadline: two business days</h2>
<p>Since the Sarbanes-Oxley Act of 2002, a Form 4 must be filed <b>before the end of the second business day</b> after
the trade. That is why insider data is useful: you usually learn about a purchase within days, not months. Each table
on this site shows both dates. <i>Trade</i> is when the shares changed hands; <i>Filed</i> is when the SEC received the
form. Late filings do happen, and a gap of weeks between the two dates is worth noticing.</p>

<h2>The header: who, where and in what role</h2>
<p>The top of the form names the reporting person, the company and its ticker (the “issuer”), and the relationship
between them: director, officer (with the title written out), 10% owner, or “other”. One filing can cover several
people who share the same shares, for example a fund and the partners who control it. On Ticker Press those joint
filings are counted once, so the same purchase is not added up twice.</p>

<h2>Table I, column by column</h2>
<p>Table I covers ordinary shares (“non-derivative securities”). This is where open-market buying shows up:</p>
<ol>
<li><b>Title of security</b>: usually “Common Stock”.</li>
<li><b>Transaction date</b> (and, rarely, a deemed execution date).</li>
<li><b>Transaction code</b>: a single letter that says what kind of trade it was. This is the most important column;
see the next section.</li>
<li><b>Amount, (A) or (D), and price</b>: how many shares were acquired (A) or disposed of (D), and at what price.
When an insider buys in several lots on the same day, the filing often shows a weighted average price with a
footnote giving the range.</li>
<li><b>Amount owned after the transaction</b>: the holding after the trade. Comparing it with the amount bought gives
the stake change you see in our tables. A purchase that doubles a holding means more than one that adds 1%.</li>
<li><b>Ownership form</b>: <b>D</b> for direct or <b>I</b> for indirect.</li>
<li><b>Nature of indirect ownership</b>: if the shares are held indirectly, who holds them: a trust, an LLC, a spouse,
a fund. We show this as the <span class="tag">indirect</span> tag.</li>
</ol>
<p>Table II covers derivatives: options, warrants, convertible notes and restricted stock units. Grants and option
exercises live there, and they are not purchases with the insider's own cash.</p>

<h2>Transaction codes that matter</h2>
<p>The SEC defines about twenty codes. A handful explain almost every filing:</p>
<div class="tbl"><table><thead><tr><th>Code</th><th>Meaning</th><th>Is it a buy with own money?</th></tr></thead><tbody>
<tr><td><b>P</b></td><td>Open market or private purchase</td><td><b>Yes</b>: this is what Ticker Press tracks</td></tr>
<tr><td><b>S</b></td><td>Open market or private sale</td><td>No, it is a sale</td></tr>
<tr><td><b>A</b></td><td>Grant or award from the company</td><td>No, shares received as compensation</td></tr>
<tr><td><b>M</b></td><td>Exercise or conversion of a derivative (exempt)</td><td>No, usually options turning into shares</td></tr>
<tr><td><b>F</b></td><td>Shares withheld to pay an exercise price or taxes</td><td>No</td></tr>
<tr><td><b>G</b></td><td>Gift</td><td>No</td></tr>
<tr><td><b>D</b></td><td>Disposition back to the company</td><td>No</td></tr>
<tr><td><b>J</b></td><td>Other (explained in a footnote)</td><td>Read the footnote</td></tr>
</tbody></table></div>
<p>Most Form 4 filings are grants, option exercises and tax withholding: routine paperwork around pay packages. A
code <b>P</b> purchase is different because the insider wrote a cheque at the market price.</p>

<h2>Read the footnotes</h2>
<p>The footnotes at the bottom are where the details hide, and they can change the meaning of a trade completely:</p>
<ul>
<li><b>Price ranges</b>: the high and low prices behind an average price.</li>
<li><b>Who really holds the shares</b>: a family trust, a fund managed by the director, a company they control.</li>
<li><b>Private placements and offerings</b>: code P also covers private purchases. If the footnotes say the shares
were bought in a registered offering or a private placement, the buyer took newly issued shares from the company,
often alongside other investors, rather than buying on the market. We mark these
<span class="tag">private</span>.</li>
<li><b>Trading plans</b>: since April 2023 the form has a checkbox for trades made under a Rule 10b5-1 plan, a
schedule set up in advance. We tag those <span class="tag">10b5-1</span>; see
<a href="/guides/10b5-1-plans-explained/">our guide to 10b5-1 plans</a>.</li>
</ul>

<h2>A real example from this week</h2>
{example}

<h2>Where to find the filings</h2>
<p>Every purchase on Ticker Press links to its original filing on the SEC's EDGAR system (the “SEC” link at the end
of each row). You can also search any company on <a href="https://www.sec.gov/edgar/search/" rel="nofollow">EDGAR
full-text search</a> and filter by form type “4”. If a number on this site ever looks wrong, the filing is the
source of truth, and we would like to hear about it on our <a href="/contact/">contact page</a>.</p>
""")

SIGNAL = ("why-insider-buying-matters", "Why Insider Buying Matters (and When It Doesn't)",
          "What research says about insider purchases, why buys are a stronger signal than sales, and the "
          "kinds of insider buys that tell you little.", """
<p>Insiders know their company better than anyone outside it. They see the order book, the product pipeline and the
cash position every week. When one of them takes their own money and buys more shares on the open market, it is
reasonable to ask why. This guide explains what an insider purchase can and cannot tell you.</p>

<h2>Buys say more than sales</h2>
<p>There are many reasons to sell a stock: paying taxes on vested shares, buying a house, diversifying a fortune
that sits in one company, a divorce. There is basically one reason to buy it on the open market: you think it is
worth more than the price. That asymmetry is why Ticker Press tracks purchases only.</p>

<h2>What academic research has found</h2>
<p>Insider trading data has been studied for decades. Two widely cited findings are useful to keep in mind:</p>
<ul>
<li>Josef Lakonishok and Inmoo Lee (<i>“Are Insider Trades Informative?”</i>, Review of Financial Studies, 2001)
found that insider purchases, not sales, were followed by better-than-average returns, and that the effect was
concentrated in smaller companies.</li>
<li>Lauren Cohen, Christopher Malloy and Lukasz Pomorski (<i>“Decoding Inside Information”</i>, Journal of Finance,
2012) separated “routine” insiders, who trade at the same time every year, from “opportunistic” ones. The routine
trades carried no useful information; the opportunistic ones did.</li>
</ul>
<p>Both studies describe averages over thousands of trades. They do not mean that any single insider purchase will
make money, and markets change once an effect is widely known.</p>

<h2>Signals that tend to carry more weight</h2>
<ul>
<li><b>Several insiders buying at once.</b> One person can be wrong or have a personal reason. When the CEO, the CFO
and two directors all buy in the same month, that is a <a href="/guides/what-is-a-cluster-buy/">cluster buy</a>.</li>
<li><b>Size relative to the insider's holding.</b> The “Stake” column shows how much a purchase increased the
insider's position. Doubling a stake is a stronger statement than adding 2%.</li>
<li><b>Size relative to the person.</b> $50,000 means more from a director of a small company than from a
billionaire founder.</li>
<li><b>Buying after bad news or a falling price.</b> Insiders buying into weakness are telling you they disagree with
the market. They can still be early, or wrong.</li>
<li><b>Officers with operating knowledge.</b> A CFO or CEO usually knows more about the next quarter than an outside
director.</li>
</ul>

<h2>Buys that tell you less</h2>
<ul>
<li><b>Pre-scheduled trades</b> under a <a href="/guides/10b5-1-plans-explained/">Rule 10b5-1 plan</a>. The decision
was made months earlier.</li>
<li><b>Private placements and offerings</b> (our <span class="tag">private</span> tag). The insider bought new shares
from the company, often as part of a financing round that needed their participation.</li>
<li><b>Funds that are 10% owners.</b> A venture or activist fund adding to a position can be a strong view, but it
can also be routine portfolio management, follow-on investments or support for a company in trouble. Their
reasons are different from an executive's.</li>
<li><b>Token purchases.</b> Small buys right after joining a board, or the same small amount every year, are often
about showing commitment rather than a view on the price.</li>
<li><b>Penny stocks.</b> At very low prices, a purchase can be a lifeline to a struggling company rather than a bet
on a recovery.</li>
</ul>

<h2>How to use insider data well</h2>
<p>Treat an insider purchase as a reason to look closer, not a reason to buy. Read the filing, check the footnotes,
look at the company's recent results and debt, and ask why the insider might be buying now. Our “Since buy” column
shows how past purchases have done, but past results do not predict future ones.</p>
<p>Ticker Press is information, not advice. Insiders are often early and sometimes simply wrong.</p>
""")

CLUSTER = ("what-is-a-cluster-buy", "What Is an Insider Cluster Buy?",
           "A cluster buy is when several different insiders of the same company buy its stock within a short "
           "window. How we define it, why it is watched, and its limits.", """
<p>A <b>cluster buy</b> happens when two or more different insiders of the same company buy its stock on the open
market within a short period. On Ticker Press the window is {cluster_days} days, and the
<a href="/cluster-buys/">cluster buys page</a> lists every current one, updated daily.</p>

<h2>Why several buyers matter more than one</h2>
<p>A single insider purchase can have personal reasons: a new director showing commitment, an executive reinvesting a
bonus, someone who simply likes the stock. When several people with different roles and different information reach
the same conclusion at the same time, personal reasons become less likely to explain it. Research on insider trading
has generally found that purchases by multiple insiders carry more information than isolated ones.</p>

<h2>How we build the list</h2>
<ul>
<li>Only open-market and private purchases reported with transaction code P count.</li>
<li>We count <b>different insiders</b>, not purchases: one director buying five times is still one insider.</li>
<li>Joint filings (for example a fund and its general partner reporting the same shares) count once.</li>
<li>The list is sorted by the number of insiders, then by the total amount bought.</li>
</ul>

<h2>How to read a cluster</h2>
<p>Not every cluster is equally strong. Look at:</p>
<ul>
<li><b>Who is buying.</b> A CEO and a CFO together usually say more than two outside directors.</li>
<li><b>How much.</b> Compare the amounts with each person's existing stake. Our stock pages show the stake change for
every purchase.</li>
<li><b>The tags.</b> Several insiders joining the same private placement (<span class="tag">private</span>) is a
financing event, not a series of independent decisions.</li>
<li><b>The timing.</b> Clusters right after earnings, a large price drop or a strategic announcement are common. Ask
what the insiders might be reacting to.</li>
</ul>

<h2>Limits</h2>
<p>Clusters can be coordinated: boards sometimes buy together after bad news to show confidence, and that can be as
much about public relations as conviction. Insiders can also be early by many months. A cluster is a starting point
for research, not a conclusion.</p>
""")

PLANS = ("10b5-1-plans-explained", "Rule 10b5-1 Trading Plans Explained",
         "What a Rule 10b5-1 trading plan is, the 2023 rule changes, and why pre-scheduled insider trades say "
         "less than discretionary ones.", """
<p>Insiders are almost always in possession of information the public does not have, which makes it hard for them to
trade at all without risking an insider-trading accusation. <b>Rule 10b5-1</b> under the Securities Exchange Act offers
a way out: an insider can set up a written trading plan in advance, at a time when they have no material non-public
information, and let the trades happen later according to that plan.</p>

<h2>How a plan works</h2>
<p>The plan fixes the trades in advance: amounts, prices or a formula, and dates. A broker then executes them without
further input from the insider. Because the decision was made earlier, a trade under a valid plan can take place even
if the insider has since learned something important.</p>

<h2>The 2023 changes</h2>
<p>In December 2022 the SEC tightened the rule, with the changes taking effect in 2023. The main ones:</p>
<ul>
<li><b>Cooling-off periods.</b> Directors and officers must wait before the first trade under a new or modified plan:
at least 90 days, or longer if the company's next quarterly results come later (up to a maximum of 120 days).</li>
<li><b>Certifications.</b> Directors and officers must certify, when adopting a plan, that they are not aware of
material non-public information and are acting in good faith.</li>
<li><b>Fewer overlapping plans</b> and limits on single-trade plans.</li>
<li><b>Disclosure.</b> Companies report plan adoptions and terminations by their directors and officers in their
quarterly reports, and Forms 4 and 5 now include a checkbox to show whether a trade was made under a 10b5-1 plan.</li>
</ul>

<h2>Why it matters for insider buying</h2>
<p>Most 10b5-1 plans are used for selling, but some insiders buy through them too. A purchase under a plan tells you
what the insider thought when the plan was set up, often months before the trade, not what they think today. For
that reason we tag these trades <span class="tag">10b5-1</span> and treat them as a weaker signal than discretionary
purchases, where the insider chose the day and the price.</p>

<h2>How we detect them</h2>
<p>We use the Rule 10b5-1 checkbox on the Form 4. Filings made before the checkbox existed, and filings where it was
left blank, do not show the tag even if a plan was used (sometimes only a footnote mentions it), so treat the tag as a
minimum count rather than a complete one.</p>
""")

GUIDES = [FORM4, SIGNAL, CLUSTER, PLANS]
