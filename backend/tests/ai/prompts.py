dvdrental_test_prompts = [
    {
        "prompt": "What's the revenue trend for each store by quarter in 2005, and how does it compare to the previous quarter's performance? Include the percentage change.",
        "expected_data_model": {
            "tables": ["payment", "rental", "store", "staff"],
            "joins": ["payment.rental_id=rental.rental_id", "rental.staff_id=staff.staff_id", "staff.store_id=store.store_id"],
            "time_analysis": True,
            "aggregations": True
        },
        "expected_result": "Quarterly revenue by store with QoQ % change"
    },
    {
        "prompt": "Which films have the highest profit margin considering their replacement cost versus rental revenue? Show only films that have been rented at least 20 times.",
        "expected_data_model": {
            "tables": ["film", "inventory", "rental", "payment"],
            "joins": ["film.film_id=inventory.film_id", "inventory.inventory_id=rental.inventory_id", "rental.rental_id=payment.rental_id"],
            "calculations": True,
            "having": True
        },
        "expected_result": "Film profitability analysis with minimum rental threshold"
    },
    {
        "prompt": "Identify customer segments based on their rental frequency and average spending per rental. Categorize them into 'Premium', 'Regular', and 'Occasional' customers. Show the size and total revenue for each segment.",
        "expected_data_model": {
            "tables": ["customer", "rental", "payment"],
            "joins": ["customer.customer_id=rental.customer_id", "rental.rental_id=payment.rental_id"],
            "case_statements": True,
            "aggregations": True
        },
        "expected_result": "Customer segmentation analysis"
    },
    {
        "prompt": "What's the inventory turnover rate for each film category in each store? Include films that haven't been rented in the last 30 days as potential candidates for removal.",
        "expected_data_model": {
            "tables": ["film", "film_category", "category", "inventory", "rental", "store"],
            "joins": ["film.film_id=film_category.film_id", "film_category.category_id=category.category_id", 
                     "film.film_id=inventory.film_id", "inventory.inventory_id=rental.inventory_id"],
            "date_filters": True,
            "subqueries": True
        },
        "expected_result": "Inventory turnover analysis by category and store"
    },
    {
        "prompt": "Generate a report showing the staff performance metrics including average transaction value, number of rentals processed, and customer satisfaction (based on return time compliance). Rank staff members based on these metrics.",
        "expected_data_model": {
            "tables": ["staff", "rental", "payment", "customer"],
            "joins": ["staff.staff_id=rental.staff_id", "rental.rental_id=payment.rental_id", 
                     "rental.customer_id=customer.customer_id"],
            "window_functions": True,
            "multiple_metrics": True
        },
        "expected_result": "Staff performance dashboard with rankings"
    },
    {
        "prompt": "Which film categories show seasonal popularity? Analyze the rental patterns across different months and identify peak seasons for each category. Include year-over-year growth rates.",
        "expected_data_model": {
            "tables": ["rental", "inventory", "film", "film_category", "category"],
            "joins": ["rental.inventory_id=inventory.inventory_id", "inventory.film_id=film.film_id",
                     "film.film_id=film_category.film_id", "film_category.category_id=category.category_id"],
            "time_series": True,
            "seasonality": True
        },
        "expected_result": "Category seasonality analysis with YoY growth"
    },
    {
        "prompt": "Create a customer retention analysis showing the percentage of customers who continue to rent movies each month after their first rental. Break this down by customer city and film preferences.",
        "expected_data_model": {
            "tables": ["customer", "address", "city", "rental", "inventory", "film", "film_category", "category"],
            "joins": ["customer.address_id=address.address_id", "address.city_id=city.city_id",
                     "customer.customer_id=rental.customer_id", "rental.inventory_id=inventory.inventory_id",
                     "inventory.film_id=film.film_id", "film.film_id=film_category.film_id"],
            "cohort_analysis": True,
            "retention_metrics": True
        },
        "expected_result": "Customer retention analysis by location and preferences"
    },
    {
        "prompt": "What's the impact of film length on rental duration and late returns? Calculate the correlation between movie duration and rental patterns, segmented by rating and category.",
        "expected_data_model": {
            "tables": ["film", "inventory", "rental", "film_category", "category"],
            "joins": ["film.film_id=inventory.film_id", "inventory.inventory_id=rental.inventory_id",
                     "film.film_id=film_category.film_id", "film_category.category_id=category.category_id"],
            "correlation": True,
            "statistical_analysis": True
        },
        "expected_result": "Film duration impact analysis on rental patterns"
    }
]
