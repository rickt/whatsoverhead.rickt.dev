# whatsoverhead
what aircraft is overhead?

live demo: [https://whatsoverhead.rickt.dev](https://whatsoverhead.rickt.dev)

## what is it

a self-contained python app that reports if any aircraft are overhead of a given location / set of coordinates. 

uses the free [adsb.fi](https://adsb.fi) [ADS-B API](https://github.com/adsbfi/opendata/blob/main/README.md). 

* static assets (frontend HTML/JS, a PNG and an .ico) are in the [static](https://github.com/rickt/whatsoverhead/tree/main/static) and [templates](https://github.com/rickt/whatsoverhead/tree/main/templates) folders
* (old) scripts to build/push/deploy to GCP Cloud Run are in [scripts](https://github.com/rickt/whatsoverhead/tree/main/scripts)
* i deploy automatically on commits to GCP Cloud Run using a [workflow](https://github.com/rickt/whatsoverhead/tree/main/.github/workflows) but you can put it wherever. 

## how it works
1. use as a webpage
* `/` home page or base URL renders the web page HTML/JS from the [templates](https://github.com/rickt/whatsoverhead/tree/main/templates) folder
* asks user to allow giving their location to the webpage 
* shows the user if any aircraft are overhead
2. use as an API
* `/nearest_plane` URL takes parameters and returns JSON or text as you prefer

## API endpoints
* render web page
  * **GET** `/`
  * description:
    * renders the home page of the app

* health check
  * **GET** `/health`
  * description:
    * returns health status of the API
  * parameters:
    * none
  * response:
    ```
    {
       "status": "healthy"
    }
    ```

* nearest plane
  * **GET** `/nearest_plane`
  * description:
    * returns the nearest aircraft to the given coordinates within a specified distance
     * parameters:
       ```
          | Name   | Type   | Default | Description                           |
          |--------|--------|---------|---------------------------------------|
          | lat    | float  | None    | Latitude of the location (required).  |
          | lon    | float  | None    | Longitude of the location (required). |
          | dist   | float  | 5.0     | Search radius in kilometers.          |
          | max_alt| float  | None    | Max altitude in feet (optional).      |
          | movement| string| None    | Filter: 'receding' or 'approaching'.  |
          | format | string | json    | Response format (json or text).       |
       ```
      * responses:
        * text:
          ```
          ABC123 is a Boeing 737-800 operated by Airline Inc. at bearing 270º (west), 3.2 kilometers away at 35000ft, speed 500 knots, ground track 270º, receding at 120 knots.
          ```
        * JSON:
          ```
          {
            "flight": "ABC123",
            "desc": "Boeing 737-800",
            "alt_baro": "35000",
            "alt_geom": 35000,
            "gs": 500,
            "track": 270,
            "year": 2023,
            "ownop": "Airline Inc.",
            "distance_km": 3.2,
            "bearing": 270,
            "relative_speed_knots": 120,
            "message": "ABC123 is a Boeing 737-800 operated by Airline Inc. at bearing 270º (west), 3.2 kilometers away at 35000ft, speed 500 knots, ground track 270º, receding at 120 knots."
          }
          ```
        * error example:
          ```
          {
             "detail": "Error fetching data from ads-b API: Timeout occurred."
          }
          ```

## notes
* the github [workflow](https://github.com/rickt/whatsoverhead/tree/main/.github/workflows) has separate deploy logic for commits to dev or main

## inspiration
inspiration for this came from John Wiseman's [whatsoverhead.com](https://whatsoverhead.com), which i loved! i wanted to know how it works and ended up writing my own version. 

