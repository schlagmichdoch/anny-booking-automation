import requests
import termtables as tt

from requests.exceptions import JSONDecodeError
from config.constants import RESOURCE_URL, BOOKING_API_BASE, CHECKOUT_FORM_API, ANNY_BASE_URL, SERVICE_ID
from re import sub
from utils.helpers import get_future_datetime, extract_html_value


class CheckoutException(Exception):
    pass


class BookingClient:
    def __init__(self, cookies):
        self.session = requests.Session()
        self.session.cookies = cookies
        self.token = cookies.get('anny_shop_jwt')

        self.session.headers.update({
            'authorization': f'Bearer {self.token}',
            'accept': 'application/vnd.api+json',
            'content-type': 'application/vnd.api+json',
            'origin': ANNY_BASE_URL,
            'referer': ANNY_BASE_URL + '/',
            'user-agent': 'Mozilla/5.0'
        })

    def get_all_resources(self):
        response_planner = self.session.get(
            f"{ANNY_BASE_URL}/en/planner",
        )

        if not response_planner.ok:
            print(f"❌ Failed to fetch planner: HTTP {response_planner.status_code}")
            return None

        customer_account_id = extract_html_value(
            response_planner.text,
            r'id="__NUXT_DATA__".*"customer-accounts","([^"]+)"'
        )

        response_locations = self.session.get(
            "https://b.anny.eu/api/v1/resources/locations",
            params = {
                "filter[include_unavailable]": "1",
                "filter[customer_account_id]": customer_account_id,
                "filter[exclude_child_resources]": "0",
                "filter[use_view_options]": "1",
                "filter[start_date]": get_future_datetime(0, "00:00:00"),
                "filter[end_date]": get_future_datetime(0, "23:59:59")
            }
        )
        if not response_locations.ok:
            print(f"❌ Failed to fetch resources: HTTP {response_locations.status_code}")
            return None

        try:
            locations = response_locations.json()
        except (ValueError, JSONDecodeError):
            print(f"❌ Invalid JSON response when fetching resources: {response_locations.text[:200]}")
            return None

        ids = {}
        for d in locations['resourceLocations']:
            ids.update({
                int(d["id"]): {
                    "name": d["name"],
                    "service_id": next(iter(d["availabilityResult"]["all_ranges_exact_match"])),
                    "resource_id": d["id"],
                    "resource_url_path": f"/resources/{d["parentSlug"]}/children" if d["parentSlug"] else None
                }
            })

        # sort after resource_ids
        ids = {key: value for key, value in sorted(ids.items())}

        # sort after service id
        ids = {key: value for key, value in
                       sorted(ids.items(), key=lambda item: int(sub(r"\D", "", item[1]["service_id"]) or 0))}

        return ids

    def print_all_resources(self):
        ids = self.get_all_resources()
        ids_tab = [
            [ids[i]['name'].replace("|", "\\|"), ids[i]['resource_id'], ids[i]['service_id'],
             ids[i]['resource_url_path']]
            for i in ids]

        # print available resources
        tt.print(
            ids_tab,
            header=['name', 'resource_id', 'service_id', 'resource_url_path'],
            style=tt.styles.markdown,
        )

    def find_available_resources(self, start, end):
        response = self.session.get(RESOURCE_URL, params={
            'page[number]': 1,
            'page[size]': 250,
            'filter[available_from]': start,
            'filter[available_to]': end,
            'filter[availability_exact_match]': 1,
            'filter[exclude_hidden]': 0,
            'filter[exclude_child_resources]': 0,
            'filter[availability_service_id]': int(SERVICE_ID),
            'filter[include_unavailable]': 0,
            'filter[pre_order_ids]': '',
            'sort': 'name'
        })
        if not response.ok:
            print(f"❌ Failed to fetch resources: HTTP {response.status_code}")
            return None
        try:
            resources = response.json().get('data', [])
        except (ValueError, JSONDecodeError):
            print(f"❌ Invalid JSON response when fetching resources: {response.text[:200]}")
            return None
        return [r['id'] for r in resources]

    def reserve(self, resource_id, start, end):
        booking = self.session.post(
            f"{BOOKING_API_BASE}/order/bookings",
            params={
                'stateless': '1',
                'include': 'customer,voucher,bookings.booking_add_ons.add_on.cover_image,bookings.sub_bookings.resource,bookings.sub_bookings.service,bookings.customer,bookings.service.custom_forms.custom_fields,bookings.service.add_ons.cover_image,bookings.service.add_ons.group,bookings.cancellation_policy,bookings.resource.cover_image,bookings.resource.parent,bookings.resource.location,bookings.resource.category,bookings.reminders,bookings.booking_series,bookings.sequenced_bookings.resource,bookings.sequenced_bookings.service,bookings.sequenced_bookings.service.add_ons.cover_image,bookings.sequenced_bookings.service.add_ons.group,bookings.booking_participants,sub_orders.bookings,sub_orders.organization.legal_documents'
            },
            json={
                "resource_id": [resource_id],
                "service_id": {SERVICE_ID: 1},
                "start_date": start,
                "end_date": end,
                "description": "",
                "customer_note": "",
                "add_ons_by_service": {SERVICE_ID: [[]]},
                "sub_bookings_by_service": {},
                "strategy": "multi-resource"
            }
        )

        if not booking.ok:
            print(f"❌ Booking failed: HTTP {booking.status_code}")
            try:
                errors = booking.json().get("errors", {})
                print(f"  {errors[0]['title']}: {errors[0]['detail']}")
                print(f"  resource_id: {resource_id}; start: {start}; end: {end}")
            except:
                pass
            return False

        try:
            data = booking.json().get("data", {})
        except (ValueError, JSONDecodeError):
            print("❌ Invalid JSON response from booking request.")
            print(f"  resource_id: {resource_id}; start: {start}; end: {end}")
            print(f"  response: {booking.text[:200]}")
            return False

        oid = data.get("id")
        oat = data.get("attributes", {}).get("access_token")

        if not oid or not oat:
            print("❌ Missing booking ID or access token in response")
            return False

        checkout = self.session.get(f"{CHECKOUT_FORM_API}?oid={oid}&oat={oat}&stateless=1")
        if not checkout.ok:
            print(f"❌ Checkout form failed: HTTP {checkout.status_code}")
            return False

        try:
            customer = checkout.json().get("default", {}).get("customer", {})
        except (ValueError, JSONDecodeError):
            print(f"❌ Invalid JSON response from checkout form: {checkout.text[:200]}")
            return False

        final = self.session.post(
            f"{BOOKING_API_BASE}/order",
            params={
                "stateless": "1",
                "include": "customer,voucher,bookings.booking_add_ons.add_on.cover_image,bookings.sub_bookings.resource,bookings.sub_bookings.service,bookings.customer,bookings.service.custom_forms.custom_fields,bookings.service.add_ons.cover_image,bookings.service.add_ons.group,bookings.cancellation_policy,bookings.resource.cover_image,bookings.resource.parent,bookings.resource.location,bookings.resource.category,bookings.reminders,bookings.booking_series,bookings.sequenced_bookings.resource,bookings.sequenced_bookings.service,bookings.sequenced_bookings.service.add_ons.cover_image,bookings.sequenced_bookings.service.add_ons.group,bookings.booking_participants,sub_orders.bookings,sub_orders.organization.legal_documents",
                "oid": oid,
                "oat": oat
            },
            json={
                "customer": {
                    "given_name": customer.get("given_name"),
                    "family_name": customer.get("family_name"),
                    "email": customer.get("email")
                },
                "accept_terms": True,
                "payment_method": "",
                "success_url": f"{ANNY_BASE_URL}/checkout/success?oids={oid}&oats={oat}",
                "cancel_url": f"{ANNY_BASE_URL}/checkout?step=checkout&childResource={resource_id}",
                "meta": {"timezone": "Europe/Berlin"}
            }
        )

        if not final.ok:
            print(f"❌ Checkout failed: HTTP {final.status_code}")
            try:
                errors = final.json().get("errors", {})
                print(f"  {errors[0]['title']}: {errors[0]['detail']}")
                print(f"  resource_id: {resource_id}; start: {start}; end: {end}")
            except:
                pass

            # Clear checkout cart
            clear_checkout = self.session.get(
                f"{BOOKING_API_BASE}/order/bookings/delete-all",
                params={
                    "stateless": "1",
                    "include": "customer,voucher,bookings.booking_add_ons.add_on.cover_image,bookings.sub_bookings.resource,bookings.sub_bookings.service,bookings.customer,bookings.service.custom_forms.custom_fields,bookings.service.add_ons.cover_image,bookings.service.add_ons.group,bookings.cancellation_policy,bookings.resource.cover_image,bookings.resource.parent,bookings.resource.location,bookings.resource.category,bookings.reminders,bookings.booking_series,bookings.sequenced_bookings.resource,bookings.sequenced_bookings.service,bookings.sequenced_bookings.service.add_ons.cover_image,bookings.sequenced_bookings.service.add_ons.group,bookings.booking_participants,sub_orders.bookings,sub_orders.organization.legal_documents",
                    "oid": oid,
                    "oat": oat
                }
            )
            if clear_checkout.ok:
                print(f"  Checkout cart has been cleared. Booking quota should be restored.")
            else:
                print(f"  Checkout cart could not be cleared. You might need to wait 15 minutes for your booking quota to be restored automatically again.")

            raise CheckoutException

        print("✅ Reservation successful!")
        print(f"  resource_id: {resource_id}; start: {start}; end: {end}")
        return True