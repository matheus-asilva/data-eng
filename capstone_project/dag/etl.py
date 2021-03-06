import os
from datetime import datetime
from pyspark.sql import SparkSession, Row, Window
from pyspark.sql.functions import udf, col, row_number, translate
from pyspark.sql.functions import year, month, dayofmonth, hour, weekofyear, date_format, dayofweek
from pyspark.sql.functions import arrays_zip, explode
from pyspark.sql.types import IntegerType, TimestampType
from pyspark.sql.functions import explode, regexp_replace
from pyspark import SparkConf
from pyspark import SparkContext

def read_and_clean_json(spark, input_json_path):
    '''
    Reads json file with products data
    Explodes nested columns
    Selects main columns to dataframe
    Replaces wrong values of character '&'(ampersand)
    Replaces values of character '$' to cast price column to double
    Replaces wrong null values to None
    Casts price column to double
    '''
    df = spark.read.json(input_json_path)    
    df = df.withColumn("tmp", arrays_zip("category", "description", "image")) \
        .withColumn("tmp", explode("tmp")) \
        .select("asin", col("tmp.category"), col("tmp.description"), col("tmp.image"), "title", "brand", "main_cat", "price") \
        .withColumn('brand', translate('brand', '&amp;', '&')) \
        .withColumn('category', translate('category', '&amp;', '&')) \
        .withColumn('main_cat', translate('main_cat', '&amp;', '&')) \
        .withColumn('price', regexp_replace('price', '\$', '')) \
        .replace(['null', '', 'None'], None) \
        .withColumn('price', col('price').cast("double"))
    return df

def category_data(df, output_path, s3_partition):
    '''
    Creates dataframe of product category to be a dimension table (normalized)
    Selects distinct categories and drop duplicates
    Creates category_id with window function
    Writes .parquet file to S3 
    '''
    window = Window.orderBy(col('category'))
    category_table = df.select(['category']) \
                    .where(col('category').isNotNull()) \
                    .dropDuplicates() \
                    .withColumn('category_id', row_number().over(window))

    category_table.repartition(5).write.parquet(output_path+'category/'+s3_partition, 'overwrite')
    return category_table

def brand_data(df, output_path,s3_partition):
    '''
    Creates dataframe of product brand to be a dimension table (normalized)
    Selects distinct brands and drop duplicates
    Creates brand_id with window function
    Writes .parquet file to S3 
    '''
    window = Window.orderBy(col('brand'))
    brand_table = df.select(['brand']) \
                    .where(col('brand').isNotNull()) \
                    .dropDuplicates() \
                    .withColumn('brand_id', row_number().over(window))
    brand_table.repartition(5).write.parquet(output_path+'brand/'+s3_partition, 'overwrite')
    return brand_table

def main_category_data(df, output_path, s3_partition):
    '''
    Creates dataframe of product main category to be a dimension table (normalized)
    Selects distinct main categories and drop duplicates
    Creates main_cat_id with window function
    Writes .parquet file to S3 
    '''
    window = Window.orderBy(col('main_cat'))
    main_cat_table = df.select(['main_cat']) \
                        .where(col('main_cat').isNotNull()) \
                        .dropDuplicates() \
                        .withColumn('main_cat_id', row_number().over(window))
    main_cat_table.repartition(5).write.parquet(output_path+'main_category/'+s3_partition, 'overwrite')
    return main_cat_table

def product_data(df, output_path, s3_partition, category_table, brand_table, main_cat_table):
    '''
    Creates dataframe of products data (normalized)
    Selects main columns
    Renames column of product_id
    Joins with other dimension tables to get their id of brand, category and main_category
    Writes .parquet file to S3 
    '''
    products = df.select('asin', 'title', 'description', 'image', 'brand', 'category', 'main_cat', 'price') \
                  .withColumnRenamed('asin', 'product_id')
             

    products_data = products.join(brand_table, on=['brand'], how='left') \
                            .join(category_table, on=['category'], how='left') \
                            .join(main_cat_table, on=['main_cat'], how='left') \
                            .drop('brand', 'category', 'main_cat')  
                            
    products_data.repartition(10).write.parquet(output_path+'products/'+s3_partition, 'overwrite')

def read_and_clean_csv(spark, input_csv_path):
    '''
    Reads csv file with ratings data
    Renames columns to intuitive names
    Uses udf function to transforme unix time to timestamp
    '''
    get_timestamp = udf(lambda x:  datetime.fromtimestamp(x).strftime('%Y-%m-%d %H:%M:%S'))
    df_csv = spark.read.csv(input_csv_path)
    df_csv = df_csv.withColumnRenamed('_c0', 'reviewer_id')\
                .withColumnRenamed('_c1', 'product_id')\
                .withColumnRenamed('_c2', 'rating')\
                .withColumnRenamed('_c3', 'timestamp') \
                .withColumn('rating', col('rating').cast("double")) \
                .withColumn('timestamp', col('timestamp').cast('integer')) 
    df_csv = df_csv.withColumn('start_time', get_timestamp(df_csv.timestamp))
    return df_csv

def time_data(df_csv, output_path, s3_partition):
    '''
    Creates dataframe of time rating data to be a dimension table (normalized)
    Creates additional columns of year, month, day, hour, weekday to dataframe
    Drops duplicates timestamps
    Writes .parquet file to S3 
    '''
    time_table =  df_csv.withColumn('year', year('start_time')) \
                    .withColumn('month', month('start_time')) \
                    .withColumn('day', dayofmonth('start_time')) \
                    .withColumn('hour', hour('start_time')) \
                    .withColumn('weekday', dayofweek('start_time')) \
                    .select('timestamp', 'start_time', 'year', 'month', 'day', 'hour', 'weekday') \
                    .dropDuplicates(["timestamp"])
    time_table.repartition(5).write.parquet(output_path+'time/'+s3_partition, 'overwrite')

def ratings_data(df_csv, output_path, s3_partition):
    '''
    Creates dataframe of ratings data to be a dimension table (normalized)
    Writes .parquet file to S3 
    '''
    ratings_table = df_csv.select(['reviewer_id', 'product_id', 'rating', 'start_time'])
    ratings_table.repartition(10).write.parquet(output_path+'ratings/'+s3_partition, 'overwrite')


def main():
    '''
    Get all environ variables of spark-env
    Create session of spark
    Call ETL functions
    '''
    output_path= os.getenv('OUTPUT_S3_PATH')
    s3_partition= os.getenv('S3_PARTITION')
    input_json_path = os.getenv('INPUT_JSON_PATH')
    input_csv_path = os.getenv('INPUT_CSV_PATH')

    spark = SparkSession \
            .builder \
            .getOrCreate()

    #JSON File and Tables
    df = read_and_clean_json(spark, input_json_path)
    category_table = category_data(df, output_path, s3_partition)
    brand_table = brand_data(df, output_path, s3_partition)
    main_cat_table = main_category_data(df, output_path, s3_partition)
    product_data(df, output_path, s3_partition, category_table, brand_table, main_cat_table)

    #CSV File and Tables
    df_csv = read_and_clean_csv(spark, input_csv_path)
    time_data(df_csv, output_path, s3_partition)
    ratings_data(df_csv, output_path, s3_partition)

if __name__ == "__main__":
    main()
