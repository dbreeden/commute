#!/usr/bin/python
import argparse
import collections
import datetime
import json
import re
import time
import urllib
import urllib2

from xml.etree import ElementTree

DIRECTIONS_URL = 'https://maps.googleapis.com/maps/api/directions/json'

# http://www.nextbus.com/xmlFeedDocs/NextBusXMLFeed.pdf
NEXTBUS_URL = 'http://webservices.nextbus.com/service/publicXMLFeed'
AGENCY = 'sf-muni'

GOOGLE_TO_NB_NAME = {
    'PowellMason': '59',
    'PowellHyde': '60',
    'California': '61',
}


def http_get(url_base, params):
    response = urllib2.urlopen("%s?%s" % (url_base, urllib.urlencode(params)))
    return response.read()

non_alphanum = re.compile('[\W_]+')


def normalize(route_tag):
    return non_alphanum.sub('', route_tag)


def find_stop(route_config, location):
    """Find the stop on the given NextBus route with the minimum
    distance to the given Google Maps location"""
    latitude = location['lat']
    longitude = location['lng']

    def distance(stop):
        return ((float(stop.get('lat')) - latitude)**2 +
                (float(stop.get('lon')) - longitude)**2)

    return min(route_config.findall('stop'), key=distance)


def get_directions(route_config, departure_tag, arrival_tag):
    """Return all the route directions that go from departure_tag to
    arrival_tag"""
    for d in route_config.findall('direction'):
        found_departure = False
        for s in d:
            stop_tag = s.get('tag')
            if stop_tag == departure_tag:
                found_departure = True
            elif found_departure and stop_tag == arrival_tag:
                yield d
                break


def get_block_time(block, stop_tag, direction):
    """Return the time of departure from the given stop."""
    all_stop_tags = [s.get('tag') for s in direction]
    stop_index = all_stop_tags.index(stop_tag)

    last_timestamp = -1
    last_index = None
    for s in block:
        timestamp = int(s.get('epochTime'))
        if timestamp < 0:
            continue

        curr_stop_tag = s.get('tag')
        if curr_stop_tag != stop_tag:
            # See if this stop is before or after the desired stop
            try:
                curr_stop_index = all_stop_tags.index(curr_stop_tag)
            except ValueError:
                continue

            if curr_stop_index < stop_index:
                # If it's before, save it
                last_timestamp = timestamp
                last_index = curr_stop_index
                continue

            # If it's after, interpolate when the bus should pass
            # through the desired stop
            if last_timestamp < 0:
                return None

            timestamp -= ((timestamp - last_timestamp) *
                          float(curr_stop_index - stop_index) /
                          (curr_stop_index - last_index))

        return datetime.datetime.fromtimestamp(timestamp / 1000).time()

    return None


def transit_departure(transit_details, route_to_config, current_time):
    # Get the route from Google
    google_route_name = transit_details['line']['short_name']

    nextbus_route_name = normalize(google_route_name)

    nextbus_route_name = GOOGLE_TO_NB_NAME.get(
        nextbus_route_name, nextbus_route_name)

    # Normalize route_tag
    try:
        route_config = route_to_config[nextbus_route_name]
    except KeyError:
        import ipdb; ipdb.set_trace()
    # Get NextBus route
    route_tag = route_config.get('tag')

    # Find the NextBus stops at the given lat/lon
    departure_loc = transit_details['departure_stop']['location']
    departure_tag = find_stop(route_config, departure_loc).get('tag')

    arrival_loc = transit_details['arrival_stop']['location']
    arrival_tag = find_stop(route_config, arrival_loc).get('tag')

    print('\t\t%s from %s to %s' % (
        transit_details['line']['short_name'],
        transit_details['departure_stop']['name'],
        transit_details['arrival_stop']['name']))

    # Get the predicted departure times for the route & stop
    predictions = ElementTree.fromstring(http_get(NEXTBUS_URL, {
        'command': 'predictions',
        'a': AGENCY,
        'r': route_tag,
        's': departure_tag,
    }))

    good_directions = tuple(
        get_directions(route_config, departure_tag, arrival_tag))
    good_tags = {d.get('tag') for d in good_directions}

    for d in predictions[0]:
        for p in d:
            # If we won't miss the predicted departure and the arrival
            # location is in this direction, return the prediction
            if p.get('dirTag') not in good_tags:
                continue
            timestamp = int(p.get('epochTime')) / 1000
            prediction_time = datetime.datetime.fromtimestamp(timestamp)
            if prediction_time >= current_time:
                print("\t\tDepart at %s" % prediction_time.time())
                return prediction_time

    # No prediction.  Find the next scheduled time

    # Get the next departure according to Google
    timestamp = transit_details['departure_time']['value']
    next_departure = datetime.datetime.fromtimestamp(timestamp)
    while next_departure < current_time:
        next_departure += datetime.timedelta(1)
    use_google = True

    # Only look at relevant schedules
    valid_schedule = {5: 'sat', 6: 'sun'}.get(current_time.weekday(), 'wkd')

    name_to_directions = collections.defaultdict(list)
    for d in good_directions:
        name_to_directions[d.get('name')].append(d)

    schedule_params = {'command': 'schedule', 'a': AGENCY, 'r': route_tag}
    for r in ElementTree.fromstring(http_get(NEXTBUS_URL, schedule_params)):
        if r.get('serviceClass') != valid_schedule:
            continue

        directions = name_to_directions.get(r.get('direction'))
        if directions is None:
            continue

        for d in directions:
            for b in r.findall('tr'):
                # Get the departure time at the stop for this block
                block_time = get_block_time(b, departure_tag, d)
                if block_time is None:
                    continue

                # Get that time for today
                scheduled_time = datetime.datetime.combine(
                    current_time.date(), block_time)

                # If it's already passed, get the time for tomorrow.
                # TODO: This is a hack; handle if today is Friday and
                # tomorrow has a different schedule for Saturday.
                while scheduled_time < current_time:
                    scheduled_time += datetime.timedelta(1)

                if scheduled_time < next_departure:
                    next_departure = scheduled_time
                    use_google = False
                break
    if next_departure < datetime.datetime.max:
        print("\t\tDepart at %s (%s)" %
              (next_departure.time(), 'Google' if use_google else 'scheduled'))
    else:
        print("\t\tERROR: Unable to find next scheduled departure")
    return next_departure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('origin')
    parser.add_argument('destination')
    parser.add_argument('--leave_at')

    args = parser.parse_args()

    # Get config info from NextBus
    route_to_config = {}
    for r in ElementTree.fromstring(
            http_get(NEXTBUS_URL, {'command': 'routeConfig', 'a': AGENCY})):
        if r.tag == 'route':
            route_to_config[normalize(r.get('tag'))] = r

    if args.leave_at is None:
        departure_time = time.time()
    else:
        departure_time = time.mktime(
            time.strptime(args.leave_at, '%m/%d/%Y %H:%M'))

    # Get routes from Google
    directions_params = {
        'origin': args.origin,
        'destination': args.destination,
        'sensor': 'false',
        'mode': 'transit',
        'departure_time': int(departure_time),
        'alternatives': 'true',
    }
    routes = json.loads(http_get(DIRECTIONS_URL, directions_params))['routes']

    best_arrival = datetime.datetime.max
    best_route = None
    for r_i, r in enumerate(routes):
        current_time = datetime.datetime.fromtimestamp(departure_time)

        print("Route %d" % (r_i + 1))

        # For each step:
        for s_i, s in enumerate(r['legs'][0]['steps']):
            print("\t%d. %s" % (s_i + 1, s['html_instructions']))
            # If it is transit:
            if s['travel_mode'] == 'TRANSIT':
                current_time = transit_departure(
                    s['transit_details'], route_to_config, current_time)
                if current_time == datetime.datetime.max:
                    break
            else:
                # Add a minute more than Google thinks for walking
                current_time += datetime.timedelta(minutes=1)

            # Add the step's duration to the current time
            current_time += datetime.timedelta(seconds=s['duration']['value'])

            print("\t\tArrive at %s" % current_time.time())

        if current_time < best_arrival:
            best_arrival = current_time
            best_route = r_i + 1

    # Output the best one
    print("Take route %d, arriving at %s" %
          (best_route, best_arrival))

if __name__ == '__main__':
    main()
