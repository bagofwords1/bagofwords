"""Write the synthetic sales_2025.csv the loop uploads (3000 orders, seeded)."""
import csv, datetime, random, sys

random.seed(7)
regions = ['North', 'South', 'East', 'West', 'Central']
cats = {'Electronics': ['Laptop', 'Phone', 'Tablet', 'Headphones'], 'Home': ['Blender', 'Lamp', 'Vacuum'],
        'Sports': ['Bike', 'Tent', 'Shoes'], 'Office': ['Chair', 'Desk', 'Printer']}
price = {'Laptop': 1200, 'Phone': 800, 'Tablet': 500, 'Headphones': 150, 'Blender': 90, 'Lamp': 45, 'Vacuum': 220,
         'Bike': 650, 'Tent': 300, 'Shoes': 120, 'Chair': 260, 'Desk': 480, 'Printer': 200}
out = sys.argv[1] if len(sys.argv) > 1 else 'sales_2025.csv'
with open(out, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['order_id', 'order_date', 'region', 'category', 'product', 'units', 'unit_price', 'revenue', 'customer_id', 'returned'])
    d0 = datetime.date(2025, 1, 1)
    for i in range(3000):
        d = d0 + datetime.timedelta(days=random.randint(0, 364))
        r = random.choice(regions); c = random.choice(list(cats)); p = random.choice(cats[c])
        growth = 1 + (d.month / 12) * (0.6 if c == 'Sports' else 0.15)
        u = max(1, int(random.gauss(3, 1.5) * growth)); up = price[p]
        w.writerow([f'O-{100000 + i}', d.isoformat(), r, c, p, u, up, round(u * up, 2),
                    f'C-{random.randint(1, 700):04d}', 'yes' if random.random() < 0.06 else 'no'])
print(out)
