from main import SessionLocal, stripe
coupon = stripe.Coupon.create(
    percent_off=40,
    duration="once",
    id="tTI70JKq",
    name="FLASH40 - 40% off first month"
)
print(coupon.id)
