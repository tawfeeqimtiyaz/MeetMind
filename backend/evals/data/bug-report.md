# Bug report PAY-301: Checkout session times out

**Severity:** High
**Reported by:** Support team
**Environment:** Web checkout, Chrome and Safari

## Steps to reproduce
1. Add any item to the cart.
2. Wait on the payment page for more than 10 minutes.
3. Press "Pay now".

## Actual result
The page shows error code **E-5102 "Session timeout"** and the cart is emptied.

## Expected result
The session is refreshed and the payment goes through.
