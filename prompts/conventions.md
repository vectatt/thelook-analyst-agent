# Business rules for this dataset — owned by the analytics team.

- Revenue = SUM(order_items.sale_price). Exclude status IN ('Cancelled','Returned') unless the
  question is about cancellations or returns.
- Never multiply sale_price by orders.num_of_item: sale_price is per item, num_of_item is per order.
- Margin = sale_price - products.cost. retail_price is a list price, not revenue.
- Compare regions per customer (revenue / COUNT(DISTINCT user_id)), never on raw totals — populations
  differ several-fold.
- Return rate = Returned items / items that were not Cancelled (cancelled items never shipped).
- An "active" customer has ordered in the last 90 days.
- Churn, retention and engagement have no agreed definition here yet. If one is asked for, say so and
  offer a concrete proxy the data does support ("customers who ordered in the 90 days before last
  month but not since") rather than picking a definition silently. Whenever you answer with a metric
  that is not defined in this list, state the definition you used in one line.
- The dataset is regenerated continuously and contains future-dated rows. Use CURRENT_DATE()-relative
  windows and always state which window was used.
