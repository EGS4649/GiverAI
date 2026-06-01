from main import SessionLocal, stripe
coupon = stripe.Coupon.modify(
    "tTI70JKq",
    name="FLASH40"
)

print(coupon.id, coupon.name)
